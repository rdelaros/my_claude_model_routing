# Model Router Plugin

Automatic model routing for Claude Code — uses the cheapest model capable of each task, and keeps tool-heavy work out of the main session's context.

## How it works

| Tier | Runs on | What it handles |
|---|---|---|
| **Thinking** | Main session (your selected model) | Architecture, design, trade-offs, discussions. Never delegated. |
| **Building** | `model-router:builder` — sonnet by default | Code edits, file creation, test writing. Runs in parallel subagents. |
| **Operational** | `model-router:operator` — haiku by default | Git, pull and merge requests (Azure DevOps, GitLab, GitHub), pipelines, Azure CLI lookups, Terraform plans, Kubernetes and Helm reads, deployments, job runs, queries, Jira/Confluence updates. |

The operator runs in the background (`background: true` in its definition). When you ask for a pull, a pipeline check or a deployment, the main session launches it, tells you in one line what is running, and hands the prompt back — you keep working and get the result when it finishes. Builders stay in the foreground of the plan they belong to, because the main session needs their results to integrate.

The plugin ships four things:

- `agents/` — the `builder` and `operator` subagents, with a default model and effort in their frontmatter.
- `rules/routing.md` — the routing rules: tiers, how to classify, when to delegate, the confirmation gate.
- `hooks/` — a `SessionStart` hook that injects `rules/routing.md` plus your recorded routing corrections into every session, a fallback that injects them on the next prompt when the session was already open, a `UserPromptSubmit` hook that tells you when `/compact` would pay off, and a `SubagentStop` hook that sends an over-long subagent report back for a summary. Nothing needs to be pasted into `CLAUDE.md`.
- `skills/setup/` — `/model-router:setup`, which checks which models answer on your provider and sets the model for each tier.

## Installation

```
/plugin marketplace add rdelaros/my_claude_model_routing
/plugin install model-router@model-routing
```

From a local clone, use the path instead: `/plugin marketplace add /path/to/my_claude_model_routing`.

Then run `/model-router:setup` once to confirm the tier models work on your provider. A session that was already open when you installed or reloaded the plugin gets the routing rules on its next prompt; new sessions get them at start. The hook needs `python3` (or `python`) on `PATH`; without it the agents still load but the routing rules are not injected.

## Slash commands, skills and other agents

A slash command or skill is routed like any other request. If its steps are commands or MCP calls — a "check my pending tasks" command that queries Jira and lists pull requests, say — the main session hands the whole command text to the operator and shows you the finished report, instead of running the steps itself. When a skill dispatches agents of its own, the main session gives them a model by the same tiers.

That routing is a judgement the main session makes, and a command that spells out its steps tempts it to just follow them. To make a command or skill of your own always run in the operator, say so in its frontmatter:

```
---
context: fork
agent: model-router:operator
---
```

`context: fork` runs the command in a subagent instead of the main conversation, `agent` picks which one, and forks run in the background by default, so the prompt comes straight back to you. Use `agent: model-router:builder` for commands that edit files.

Your own agents (`~/.claude/agents/*.md`) are not touched by the plugin: without a `model:` line in their frontmatter they run on the main model. Add `model: haiku` or `model: sonnet` to the ones that do operational or implementation work.

## Choosing the models

Not every provider serves every model: a Bedrock, Vertex or Foundry gateway may have no haiku deployment, or serve it under its own name. Run `/model-router:setup` inside the session you want to configure. It:

1. shows the provider, the main model, and the model each tier uses;
2. sends one minimal request per model through `claude -p` and reports `OK`, `FAIL` with the provider's error, or `FALLBACK` when a different model family answered than the one asked for;
3. lets you pick a working model alias (`haiku`, `sonnet`, `opus`, `fable`) for each tier;
4. if an alias fails but the provider has that model under another name, tests the id you give it and — after you agree — maps the alias to it through `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` in that config's `settings.json`, backing the file up first.

Tier choices are stored in `<config dir>/model-router/config.json`, and the session hook writes them into the routing rules, so the main session passes the right `model` on every delegation. Everything is per config directory (`CLAUDE_CONFIG_DIR`): with several configs behind different providers, run the setup once in each.

If no model cheaper than your main one is available, delegation still pays: the tool calls run over a small fresh context instead of the whole conversation, which is most of the saving.

The helper also works on its own: `python3 skills/setup/scripts/models.py show | check [model ...] | set builder=<alias> operator=<alias> | map <alias>=<model-id> | unmap <alias>`.

## Confirmation gate

Delegation is not authorization. These are confirmed with you in the main session first, unless your message already named the action and target explicitly:

- production targets, including tags and PR/MR merges that promote to pre-production or production;
- anything that changes infrastructure or cloud resources — `terraform apply`, `destroy`, `import` and state surgery; `az` create, update, delete and role assignments; `kubectl` and `helm` changes;
- SQL that writes, destroys or grants;
- cancelling or deleting jobs, pipelines, branches, tickets or remote files.

The operator refuses these actions unless its task carries the `CONFIRMED BY USER` line the main session adds after you agree.

Reads and plans are free: the operator runs `terraform plan`, `az ... show|list`, `kubectl get|describe|logs` and `glab ci status` without asking. It reports a plan as counts plus one line per resource, flagging every destroy or replace, and the main session judges whether it is safe. When an apply is confirmed, the operator applies the saved plan file the confirmation refers to, never a fresh `-auto-approve`. Before touching cloud or cluster state it reports which subscription, Terraform workspace or kube context it is in, and stops if that is not the target it was given.

Terraform, Bicep, Helm and pipeline YAML edits are Building work: the builder formats and validates what it touched and never runs a plan or apply.

## Why it routes on the work, not the wording

The rules were tuned against 816 real prompts from 37 sessions. Three findings shaped them:

- **Keyword triggers do not work.** A phrase list matched 10% of prompts. Real requests are short and contextual — "continue", "si me parece bien", a ticket URL, "tag pre and pro". The rules therefore classify the work about to be done, and an approval inherits the tier of whatever was just proposed.
- **Context is the cost, not output.** 91% of spend was the conversation being re-read or re-cached on each turn; output was under 9%. Every tool call made in a 300k-token session is billed against all 300k tokens. A subagent does the same calls on a cheaper model over a small fresh context and returns a short result.
- **Small jobs are not worth delegating.** Spawning costs about two main-session turns, so the threshold is 3+ tool calls, verbose output, or anything you would otherwise sit and wait for. By that rule roughly 70% of spend was delegable, for an estimated saving of about a third.

Operational work was 41% of prompts and 50% of cost; building 20% and 37%; thinking 39% and 13%.

## Customizing

Edit `rules/routing.md` to match your workflow — the "Typical Operational / Building / Thinking" examples are hints for the model, not match patterns. The operator's tool-specific rules (Azure CLI, Azure DevOps, Terraform, GitLab, GitHub, Kubernetes/Helm, Databricks, Jira) apply only when a task involves those tools. The operator inherits every tool the session has, including MCP servers, except the file-editing tools.

## Keeping the context small

Routing reduces what each turn costs; context size decides how big each turn is. In the measured sessions, calls made above 200k tokens of context were 26% of calls and 51% of spend, and on a gateway with a 5-minute prompt cache, 44% of spend was the conversation being re-written to cache after a pause.

**The plugin warns you.** A `UserPromptSubmit` hook (`hooks/context-watch.py`) reads the tail of the session transcript and shows a one-line message at the two moments `/compact` pays off:

- the prompt cache has expired and the context is large — the next turn re-writes everything anyway, so compacting first costs about the same and makes every later turn cheaper;
- the context first passes 150k tokens, and again at each further 50k.

Hooks cannot run `/compact` themselves. Tune or disable the warning with `MODEL_ROUTER_COMPACT_TOKENS`, `MODEL_ROUTER_COMPACT_STEP`, `MODEL_ROUTER_CACHE_TTL` (seconds; detected from the transcript by default) and `MODEL_ROUTER_CONTEXT_WATCH=0`.

**Subagent reports are kept short.** What a subagent sends back is copied into the main conversation and billed again on every later turn. The agents' own instructions ask for short reports, but other agents (`Explore`, your own, a session fork) do not follow them: in the measured sessions background reports had a median of 3,400 characters and a maximum of 18,600. A `SubagentStop` hook (`hooks/report-budget.py`) enforces a budget: when a report is over 4,000 characters (about 1,000 tokens) it saves the full text to `<config dir>/model-router/reports/<agent id>.md` and sends the subagent back once to answer with a summary that ends with `Full report: <path>`. Nothing is lost — the main session reads the file only when it needs the detail — and the rewrite costs one short turn on the subagent's own small context. It applies to every subagent, including a forked command whose report is what you read; set `MODEL_ROUTER_REPORT_CHARS` to change the budget (`0` disables) and `MODEL_ROUTER_REPORT_EXEMPT` to a comma-separated list of agent types that may report at any length. Saved reports are deleted after 14 days.

**Two settings do the rest automatically.** A plugin cannot set these; add them to `settings.json` in each config directory:

```json
{
  "autoCompactWindow": 200000,
  "promptCacheTtl": "1h"
}
```

- `autoCompactWindow` makes auto-compact start at that many tokens instead of near the model's full window — with a `[1m]` model that is otherwise close to a million. It must be an integer between 100000 and 1000000; anything else is silently ignored. The `CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable does the same.
- `promptCacheTtl: "1h"` keeps the cache warm across pauses of up to an hour. It matters on an API key, Bedrock, Vertex or Foundry, where the default is 5 minutes; a Claude subscription already gets 1 hour. 1-hour cache writes are billed at a higher rate, so it pays off only if you pause between turns — check your own pattern.

Replaying the measured gateway sessions with both settings gave an estimated 61% of actual spend (84% with the 1-hour cache alone, 72% with a 200k window alone). The estimate ignores whatever detail a compaction summary loses, so pick a window you can work with: 200k is conservative, 150k saves more.

## Parallel implementation

When you approve a plan, the main session splits it into independent work packages and launches one `builder` per package. Each builder touches only its own files and runs only its scoped tests; the main session runs the full suite once after they return. Sequential dependencies are kept sequential.

## Continuous improvement

When you correct a routing decision ("don't delegate this", "this should be haiku"), the correction is appended to `<config dir>/model-router/routing-feedback.md` (`~/.claude/model-router/` unless `CLAUDE_CONFIG_DIR` says otherwise). The exact path is shown in the injected context. The hook injects the corrections at the start of every session, in every project, and they override the shipped rules. It is plain markdown — edit or delete rows freely.

Session-start context is capped at 4,000 characters, so the hook injects the rules plus as many of the most recent corrections as fit, and points to the file for the rest. Keep `rules/routing.md` compact when you customize it — every character added there is one fewer for corrections. Once a correction has proven itself, fold it into the rules and delete the row.

## Optimising another machine

`docs/optimize-a-machine.md` is a playbook for a Claude Code session on another machine: measure how Claude Code is used there, set the two settings, install this plugin (from a copied folder if the machine cannot reach the repository), prune unused skills and plugins, and make expensive commands run in a fresh context. `tools/session_report.py` is the read-only measurement script it uses: `python3 tools/session_report.py ~/.claude [other config dirs]`.

## Requirements

- Claude Code with at least one model available for subagents. The defaults are sonnet and haiku; `/model-router:setup` checks them and lets you choose others.
- Install it in every config directory you use — each `CLAUDE_CONFIG_DIR` has its own plugin list.
- `python3` or `python` on `PATH` for the session hook
