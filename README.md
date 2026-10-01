# Model Router Plugin

Automatic model routing for Claude Code — uses the cheapest model capable of each task, and keeps tool-heavy work out of the main session's context.

## How it works

| Tier | Runs on | What it handles |
|---|---|---|
| **Thinking** | Main session (your selected model) | Architecture, design, trade-offs, discussions, judging what the agents bring back. Never delegated. |
| **Reading** | `Explore` or `model-router:operator` — haiku by default | Questions that need many files or long output: where something lives, how a feature is wired, what a 3,000-line log says. The agent returns the excerpts that answer the question with `file:line` and a one-line answer; the main session judges them. A log that needs diagnosing is Diagnosis. |
| **Building** | `model-router:builder` — sonnet by default | Code edits, file creation, scoped tests. Runs in parallel subagents. |
| **Coordination** | `model-router:coordinator` — the main model, in a fresh context | An approved plan too big for one builder: splits it into work packages, runs builders in parallel, runs the full test suite, reports once. |
| **Operational** | `model-router:operator` — haiku by default | Known commands: git (including commits with agreed text), pull and merge requests (Azure DevOps, GitLab, GitHub), pipelines, Azure CLI lookups, Terraform plans, Kubernetes and Helm reads, deployments, job runs, queries, web fetches, Jira/Confluence updates, saving a diff or log to a file. |
| **Diagnosis** | `model-router:senior-operator` — sonnet by default | Operational work that needs judgement: why a pipeline, plan or job failed, investigations across repos or resources, chains where each step decides the next, rebases that hit conflicts, and anything the operator got wrong. Reports the cause and the next step. |

The operators run in the background (`background: true` in their definitions). When you ask for a pull, a pipeline check or a deployment, the main session launches the agent, tells you in one line what is running, and hands the prompt back — you keep working and get the result when it finishes. In current Claude Code (2.1.28x) every subagent launch is asynchronous while the fork feature is on, builders and the coordinator included: a launch returns an acknowledgement and the result arrives as a completion notice. The rules and the agents are written for that: an acknowledgement is never treated as a result, and the coordinator ends its turn after launching its builders and is resumed with each one's report. `/model-router:setup` can switch the fork feature off (`forks off`), which brings the `run_in_background` parameter back so the rules can ask for a direct result from builders and the coordinator; see [Forks](#forks).

The plugin ships five things:

- `agents/` — the `builder`, `operator`, `senior-operator` and `coordinator` subagents, with a default model and effort in their frontmatter.
- `rules/routing.md` — the routing rules: tiers, how to classify, when to delegate, the confirmation gate, corrections.
- `hooks/` — `SessionStart` hooks that inject `rules/routing.md` and your recorded routing corrections into every session (on start, resume, clear, compact and fork), a fallback that injects them on the next prompt when the session was already open, a `PreToolUse` hook that gives every subagent launch its tier's model when the call omits it, a `PreToolUse` hook that keeps the operators from writing files through the shell, a `UserPromptSubmit` hook that tells you when `/compact` would pay off, a `PostToolBatch` hook that nudges the main session to delegate after a run of command or MCP calls, and a `SubagentStop` hook that sends an over-long subagent report back for a summary. Nothing needs to be pasted into `CLAUDE.md`.
- `skills/setup/` — `/model-router:setup`, which checks which models answer on your provider, sets the model for each tier, and has a `doctor` that tells you whether the router is active.
- `skills/train/` — `/model-router:train`, a training mode: the session explains each routing decision, asks you before acting, and records your corrections.
- `tests/` — a test suite that runs every hook the way Claude Code runs it, so a change to the plugin cannot silently disable routing (every hook fails open).
- `evals/` — twenty natural messages with the routing decision each should produce, for `claude plugin eval`, so a change to the rules is measured rather than trusted.

## Installation

```
/plugin marketplace add rdelaros/my_claude_model_routing
/plugin install model-router@model-routing
```

From a local clone, use the path instead: `/plugin marketplace add /path/to/my_claude_model_routing`.

Then run `/model-router:setup` once to confirm the tier models work on your provider. A session that was already open when you installed or reloaded the plugin gets the routing rules on its next prompt; new sessions get them at start. The hooks need `python3` (or `python`, 3.9 or later) on `PATH`; without it the agents still load but the routing rules are not injected, and `doctor`, being a Python script, cannot run to tell you — check `python3 --version` first.

**What you will see after installing.** Nothing changes in how you talk to Claude. When a request is operational ("rebase on main and open the MR", a pipeline URL, "what's deployed in pre"), the main session answers with one line saying which agent is running and comes back with the result when the notice arrives. When you approve a plan, it launches a builder or the coordinator instead of editing files itself. When it runs three commands itself in one turn, a reminder nudges it to delegate the rest. When the context passes 150k tokens, a one-line message suggests `/compact`. When you correct a routing decision, the correction is recorded and applied in every later session.

## Slash commands, skills and other agents

A slash command or skill is routed like any other request. If its steps are commands or MCP calls — a "check my pending tasks" command that queries Jira and lists pull requests, say — the main session hands the whole command text to the operator and shows you the finished report, instead of running the steps itself. The operator starts such a report with the line `REPORT FOR USER`, which lets it through the report budget at full length (up to four times the budget); the main session shows what follows unchanged. When a skill dispatches agents of its own, the main session gives them a model by the same tiers.

That routing is a judgement the main session makes, and a command that spells out its steps tempts it to just follow them. To make a command or skill of your own always run in the operator, say so in its frontmatter:

```
---
context: fork
agent: model-router:operator
---
```

`context: fork` runs the command in a subagent instead of the main conversation, `agent` picks which one, and forks run in the background by default, so the prompt comes straight back to you. Use `agent: model-router:builder` for commands that edit files. (This `context: fork` is a different mechanism from the `fork` subagent type below and is not affected by the fork gate.)

Your own agents (`~/.claude/agents/*.md`) are not touched by the plugin: without a `model:` line in their frontmatter they run on the main model, or on `CLAUDE_CODE_SUBAGENT_MODEL` if that is set. Add `model: haiku` or `model: sonnet` to the ones that do operational or implementation work.

**The model is set on every launch, not only asked for.** The rules tell the main session to pass the tier's `model` on each `Agent()` call, but a session under a long conversation forgets: in the measured sessions two `Explore` agents launched without a model ran on the main model for 7% of the session's cost. A `PreToolUse` hook (`hooks/agent-model.py`) completes any `Agent` call that omits `model`: the plugin's own agents get their tier's model, `Explore` gets the operator's, `Plan` and the coordinator stay on the main model, and every other agent type keeps Claude Code's default. An explicit `model` in the call always wins, and for the launches it completes the hook makes no permission decision (its only denial is `subagent_type: "fork"`, below), so prompts behave as before. To route your own agents, set `MODEL_ROUTER_AGENT_MODELS` to `agent-type=tier` pairs, e.g. `my-reviewer=senior_operator, my-scout=operator`. It runs inside subagents too, so the coordinator's builders are covered. The value is always one of the four aliases the Agent tool accepts (`haiku`, `sonnet`, `opus`, `fable`); a provider-specific model id is reached by mapping an alias (see [Choosing the models](#choosing-the-models)). `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` disables every per-agent model, the router's included; the hook then adds nothing and `doctor` warns.

**Forks are denied.** The same hook denies `subagent_type: "fork"` (a copy of the main session's whole context, on the main model, so it costs as much as doing the work in place); the deny message points to `model-router:operator`, `model-router:senior-operator`, `Explore` or `model-router:builder`. Set `MODEL_ROUTER_ALLOW_FORK=1` to allow forks. Claude Code's own prompt suggests forking while the fork feature is on, so each attempt costs a denied turn; `/model-router:setup` can turn the feature off instead ([Forks](#forks)).

**A run of commands gets a nudge.** A `PostToolBatch` hook (`hooks/ops-nudge.py`) counts the main session's Bash and MCP calls since your last prompt, failed ones included (a `UserPromptSubmit` hook resets the count). `PostToolBatch` fires once per batch after every call in it has resolved, so parallel tool calls cannot race the counter the way per-call `PostToolUse` hooks do. At the 3rd call it adds a short reminder to the model's context, naming the commands it saw, to hand the rest to the operator or senior operator in the background; at the 8th a firmer one. It never blocks or decides permissions, and skips batches that carry an `agent_id` (tool hooks inside subagents carry it; verified in 2.1.286). Set `MODEL_ROUTER_NUDGE_AT` to change the first threshold (default 3, `0` disables; the second is that plus 5). `tools/before_after.py` reports "prompts with 3+ Bash/MCP calls in main" so you can see whether it helps.

A subagent launched with a `model` keeps it when it is later resumed or messaged, so the rules allow the main session to continue a finished agent when its context is worth more than a fresh start.

## Choosing the models

Not every provider serves every model: a Bedrock, Vertex or Foundry gateway may have no haiku deployment, or serve it under its own name. Run `/model-router:setup` inside the session you want to configure. It:

1. shows the provider, the main model, the model each tier uses, the alias mappings in effect and which settings file sets them, and whether anything overrides the routing;
2. sends one tool-less, one-line probe per model through `claude -p` (a fraction of a cent each, shown in a cost column) and reports `OK`, `FAIL` with the provider's error, or `FALLBACK` when a different model family answered than the one asked for, then recommends the cheapest working alias per tier (`check --all` probes every alias);
3. lets you pick a working model alias (`haiku`, `sonnet`, `opus`, `fable`) for each tier;
4. if an alias fails but the provider has that model under another name, tests the id you give it and — after you agree — maps the alias to it through `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` in that config's `settings.json`, backing the file up first.

Tier choices are stored in `<config dir>/model-router/config.json`, and the session hook writes them into the routing rules, so the main session passes the right `model` on every delegation. Everything is per config directory (`CLAUDE_CONFIG_DIR`): with several configs behind different providers, run the setup once in each.

If no model cheaper than your main one is available, delegation still pays: the tool calls run over a small fresh context instead of the whole conversation, which is most of the saving.

The helper also works on its own: `python3 skills/setup/scripts/models.py show | doctor | check [--all] [model ...] | set builder=<alias> operator=<alias> senior_operator=<alias> | reset [tier ...] | map <alias>=<model-id> | unmap <alias> | forks off|on`.

### Doctor

`python3 skills/setup/scripts/models.py doctor` (or ask the setup skill) is the answer to "is the router doing anything?". It checks that the files are in place and what they would do: the config directory, the Python version, whether the rules file is found and how long the injected text is against the 8,000-character cap, the feedback file and its row count, whether the plugin is enabled in the settings layers it can see, the tier models (flagging an invalid stored value), the alias maps with the file each comes from, the fork gate, the forced subagent model if any, and whether `claude` is on `PATH`; it ends with the list of problems, and exits non-zero when there is one. It cannot see a `--settings` file passed on the command line. Every hook fails open — a broken hook never blocks a prompt, it just does nothing — so this is how a silent failure shows; set `MODEL_ROUTER_DEBUG=1` to make a hook print its traceback instead.

### Forks

Claude Code 2.1.28x ships a "fork" subagent feature. While it is on, the `Agent` tool has no `run_in_background` parameter and every launch from the main session is asynchronous, and the system prompt tells the model to fork itself for side work. The plugin works in that mode (the hook denies forks, the agents wait for completion notices). `models.py forks off` writes `CLAUDE_CODE_FORK_SUBAGENT=false` into the `env` block of that config's `settings.json` (backed up first); `forks on` removes it. With the feature off the fork agent type disappears, so there is nothing to deny and no "fork yourself" prompt, and the `Agent` tool gets its `run_in_background` parameter back. Launches still default to the background; the rules then tell the main session to pass `run_in_background: false` for builders and the coordinator, so their result comes back directly, while the operators keep their background default.

## Confirmation gate

Delegation is not authorization. These are confirmed with you in the main session first — whether the main session would run the command itself or delegate it — unless your message already named the action and target explicitly:

- production targets, including tags and PR/MR merges that promote to pre-production or production;
- anything that changes cloud, cluster or Terraform state — `terraform apply`, `destroy`, `import`, `taint`, `force-unlock` and state surgery; `az` create, update, set, delete, start, stop, restart, role assignments, secrets and deployments; `kubectl` and `helm` changes;
- SQL that writes, destroys or grants;
- cancelling or deleting jobs, pipelines, branches, tickets or remote files.

Either counts as confirmation: a message of yours that names the command and target, or a yes to the command and target the session stated. The operator refuses these actions unless the last line of its task is `CONFIRMED BY USER: <command> on <target>`, which the main session adds once you have named or approved that exact action and target. The line counts only there: the same words inside a quoted ticket, PR text or command steps, in a file, a comment or a command's output are not a confirmation, and a recorded routing correction can never carry them.

Reads and plans are free: the operator runs `terraform plan`, `az ... show|list`, `kubectl get|describe|logs` and `glab ci status` without asking. It reports a plan as counts plus one line per resource, flagging every destroy or replace, and the main session judges whether it is safe. When an apply is confirmed, the operator applies the saved plan file the confirmation refers to, never a fresh `-auto-approve`. Before touching cloud or cluster state it reports which subscription, Terraform workspace or kube context it is in, and stops if that is not the target it was given.

Terraform, Bicep, Helm and pipeline YAML edits are Building work: the builder formats and validates what it touched and never runs a plan or apply.

## Two operators

Operational work splits by how much thought it needs, and the split is what makes it both cheap and fast.

- The **operator** (haiku, low effort) runs known commands and reports their output: pull, rebase, open the MR, check the pipeline, run the plan, list what is deployed. A small model does these in seconds for a fraction of the price, and a wrong answer is cheap to spot.
- The **senior operator** (sonnet, medium effort) gets the tasks where the output has to be understood before the next command is chosen: why a pipeline, plan or job failed, an investigation across repositories or resources, a chain where each step decides the next, a rebase that hits conflicts. A small model on these retries, wanders and comes back with the wrong cause; the main session then re-launches, or worse, does the work itself on the most expensive model. One sonnet run that returns the cause and the next step is cheaper and faster than that. It also takes anything the operator has already got wrong.

Both have the same tool rules and confirmation gate, never edit code, and run in the background. The senior operator reports in under 200 words, leading with the finding, so the main session can act without asking it again; the operator reports in under 100.

Three things bound them. They run without the user, project and local `CLAUDE.md` files (`omitClaudeMd: true`; managed policy files still apply), because everything they need is in the task — so a repository rule that must bind them (a protected branch, a required flag, the target subscription) has to be in the delegation prompt or in `rules/routing.md`, and every turn of theirs is that much cheaper. They have a turn cap (`maxTurns`), so a cheap model that wanders stops before it costs more than the work it replaced. And they may not launch subagents or use the file tools, and a `PreToolUse` hook (`hooks/guard-writes.py`) denies the common shell ways around that for the operators and the coordinator: redirections and `tee` (also behind `sudo`, `xargs`, a path or a `VAR=x` prefix), in-place editors (`sed -i`, `perl -i`, `patch`, `truncate`), file commands with a target outside the allowed directories (`cp`, `mv`, `rm`, `touch`, `mkdir`, `dd of=`, `curl -o`, `tar x`, `unzip` and the like), git commands that rewrite the working tree or history (`apply`, `restore`, `rm`, `checkout --`, `reset --hard`, `clean`, `branch -D`, `push --force`), one-line interpreter scripts that open a file for writing, and `bash -c` strings containing any of those; the full list is in the hook's docstring. It is a safety net, not a sandbox. Writes under the session's scratchpad directory or the system temp directory are allowed: that is where an operator parks a PR diff or a long log for the main session to read in pieces. `MODEL_ROUTER_GUARD=0` disables the guard.

## Reading and reviewing

Reading files into the main session is the quiet cost: a "how does auth work here" that opens fifteen files, or a 3,000-line log pasted into the context, is paid for on every later turn. The rules route such questions to `Explore` (code: where something lives, how a feature is wired) or to the operator (logs, diffs, long docs, tickets), on the operator's model, and ask for the excerpts that answer the question, with `file:line`, plus a one-line answer; the judging stays in the main session, and a log that needs diagnosing goes to the senior operator.

Reviewing a PR or branch is the same split: the operator saves the diff and the review comments to a file under the scratchpad and returns the path with one line per changed file; the main session reads only the hunks it needs and does the judging there. Running the tests to verify a change is operational work.

## Why it routes on the work, not the wording

The rules were tuned against 816 real prompts from 37 sessions. Three findings shaped them:

- **Keyword triggers do not work.** A phrase list matched 10% of prompts. Real requests are short and contextual — "continue", "si me parece bien", a ticket URL, "tag pre and pro". The rules therefore classify the work about to be done, and an approval inherits the tier of whatever was just proposed.
- **Context is the cost, not output.** 91% of spend was the conversation being re-read or re-cached on each turn; output was under 9%. Every tool call made in a 300k-token session is billed against all 300k tokens. A subagent does the same calls on a cheaper model over a small fresh context and returns a short result.
- **Small jobs are not worth delegating.** Spawning costs about two main-session turns, so the threshold is 3+ tool calls, verbose output, or anything you would otherwise sit and wait for. One or two instant calls with short output — a command, a lookup, a small edit in a file already in context — run in the main session. By that rule roughly 70% of spend was delegable, for an estimated saving of about a third.

Operational work was 41% of prompts and 50% of cost; building 20% and 37%; thinking 39% and 13%.

## Writing for the model

The texts follow a few rules that matter more than their length. The routing rules carry four one-line examples of real messages and their tier, because examples beat descriptions for classification. Every agent report starts with `RESULT: ok` or `RESULT: failed — <reason>`, so the main session acts on one line and reads the rest only when it needs to. The main session writes agent tasks as Goal / Where / Constraints / Report as, and a `SubagentStart` hook (`hooks/agent-context.py`) adds the working directory, git branch and scratchpad path to every plugin agent at start, so the most common "missing input" round trip disappears. Tasks and commit text go to the agents in English unless you gave the text; the answer to you is in your language.

## Token footprint

Everything the plugin adds to the main session is paid for on every API call of that session, so it is kept small and measured by the tests: the injected rules are about 4,800 characters (roughly 1,300 tokens), the four agent descriptions together about 620 characters, the skill description about 330. The agents' own instructions are loaded only into the agent's small context, on its cheap model: about 1,200 tokens for the operator, 1,400 for the senior operator, 400 to 500 for the builder and coordinator. The texts follow the prompt-writing guidance Anthropic publishes: one role per agent, positive instructions, an explicit report format with a word limit, short third-person descriptions so automatic delegation picks the right agent, `effort` and `maxTurns` as the speed knobs, and nothing in a hook message beyond what the model needs at that moment.

## Customizing

Edit `rules/routing.md` to match your workflow — the classification examples are hints for the model, not match patterns. The injected text may be up to 8,000 characters (the hook keeps a margin and cuts the body at a line boundary, saying so, when it is longer; the feedback-file line always survives). The operators' tool-specific rules (Azure CLI, Azure DevOps, Terraform, GitLab, GitHub, Kubernetes/Helm, Databricks, Jira) apply only when a task involves those tools; the confirmation gate and those rules are identical in both operator files and a test keeps them so. Both operators inherit every tool the session has, including MCP servers, except the file-editing tools and `Agent`.

## Keeping the context small

Routing reduces what each turn costs; context size decides how big each turn is. In the measured sessions, calls made above 200k tokens of context were 26% of calls and 51% of spend, and on a gateway with a 5-minute prompt cache, 44% of spend was the conversation being re-written to cache after a pause.

**The plugin warns you.** A `UserPromptSubmit` hook (`hooks/context-watch.py`) reads the tail of the session transcript and shows a one-line message at the two moments `/compact` pays off:

- the prompt cache has expired and the context is large — the next turn re-writes everything anyway, so compacting first costs about the same and makes every later turn cheaper;
- the context first passes 150k tokens, and again at each further 50k.

It stays quiet right after a compaction and does not repeat a warning you have already seen. Hooks cannot run `/compact` themselves. Tune or disable the warning with `MODEL_ROUTER_COMPACT_TOKENS`, `MODEL_ROUTER_COMPACT_STEP`, `MODEL_ROUTER_CACHE_TTL` (seconds; detected from the transcript by default) and `MODEL_ROUTER_CONTEXT_WATCH=0`.

**Subagent reports are kept short.** What a subagent sends back is copied into the main conversation and billed again on every later turn. The agents' own instructions ask for short reports, but other agents (`Explore`, your own, a session fork) do not follow them: in the measured sessions background reports had a median of 3,400 characters and a maximum of 18,600. A `SubagentStop` hook (`hooks/report-budget.py`) enforces a budget: when a report is over 4,000 characters (about 1,000 tokens) it saves the full text to `<config dir>/model-router/reports/<agent id>-<time>.md` and sends the subagent back once to answer with a summary that ends with `Full report: <path>`. Nothing is lost — the main session reads the file only when it needs the detail — and the rewrite costs one short turn on the subagent's own small context. A report whose first line is `REPORT FOR USER` (a skill's finished table, meant to be shown as is) passes at up to four times the budget. Saved reports can contain whatever a command printed, so they are written readable by the owner only and deleted after 14 days. Set `MODEL_ROUTER_REPORT_CHARS` to change the budget (`0` disables) and `MODEL_ROUTER_REPORT_EXEMPT` to a comma-separated list of agent types that may report at any length.

**Two settings do the rest automatically.** A plugin cannot set these; add them to `settings.json` in each config directory:

```json
{
  "autoCompactWindow": 200000,
  "promptCacheTtl": "1h"
}
```

- `autoCompactWindow` makes auto-compact start at that many tokens instead of near the model's full window — with a `[1m]` model that is otherwise close to a million. It must be an integer between 100000 and 1000000; anything else is silently ignored. The `CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable does the same.
- `promptCacheTtl: "1h"` keeps the main conversation's cache warm across pauses of up to an hour. It matters on an API key, Bedrock, Vertex or Foundry, where the default is 5 minutes; a Claude subscription already gets 1 hour. 1-hour cache writes are billed at a higher rate, so it pays off only if you pause between turns — check your own pattern. Subagents have their own setting, `subagentPromptCacheTtl`; the operators' contexts are small and short-lived, so leave it alone.

Replaying the measured gateway sessions with both settings gave an estimated 61% of actual spend (84% with the 1-hour cache alone, 72% with a 200k window alone). The estimate ignores whatever detail a compaction summary loses, so pick a window you can work with: 200k is conservative, 150k saves more.

## Parallel implementation

When you approve a plan that fits in one or two files (and is worth a launch by the 3+ calls rule), the main session launches one `builder` with the files and the scoped tests. A bigger plan — several files or modules, or parts that must happen in order — goes whole to the `coordinator`: the plan, the repository path and the test command (the builder model only if you want to override the configured one; the hook fills it in otherwise). The coordinator runs on the main model in a fresh context. It splits the plan into work packages with no shared files, launches one `builder` per package in parallel, waits for every builder's completion notice, runs the full test suite once, sends failures back to builders for at most two fix rounds, and reports once in under 250 words: packages, tests, blockers. The main session judges that report and talks to you; it never sits through the builders' own reports.

The coordinator has no editing tools and the write guard denies the common shell write forms, so an accidental drift into editing is caught, and its `Agent` tool is limited to `model-router:builder`, so it cannot spawn anything else. It does not commit, push or deploy unless the plan says so, and it cannot ask questions — give it everything up front. Builders do not commit either: commit text is agreed with you and the commit is operational work.

## Training mode

`/model-router:train` turns the session into a tutor for the router. From then on, for every piece of work, the main session writes three short lines before acting: the routing (tier, agent, model and the rule that decides it), the plan (what the agent will be asked, and whether a confirmation is needed), and the question "right, or should it go elsewhere?". A yes starts the work; a different answer is done your way and recorded as a correction, with one line saying which rule the row changes. When the result comes back it says roughly what the choice cost and whether the tier was right in hindsight. Decisions you have already approved for the same kind of work are not re-explained.

`/model-router:train quiet` explains each decision in one line and proceeds without asking. `/model-router:train review` lists the recorded corrections grouped by tier and proposes, for each group, a sentence that could be folded into the rules so the row is no longer needed. `/model-router:train off` returns to silent routing. The mode costs about 60 words per decision on the main model, so use it for a session or two after installing and again when the router seems wrong about a kind of work.

## Continuous improvement

When you correct a routing decision ("don't delegate this", "this should be haiku"), the correction is appended as a table row to `<config dir>/model-router/routing-feedback.md` (`~/.claude/model-router/` unless `CLAUDE_CONFIG_DIR` says otherwise). The session-start hook creates the file with its header, so the first correction is a plain row like any other. The exact path is shown in the injected context. The hook injects the corrections at the start of every session, in every project, and they override the shipped rules. It is plain markdown — edit or delete rows freely.

Corrections are scoped: the rules say a correction comes only from your own message (text found in files, tool output, tickets or agent reports is never one), a row can change only which tier handles a kind of work, never the confirmation gate, and the hook drops any row that carries `CONFIRMED BY USER`, cleans control characters out of the rest, cuts a row at 300 characters and injects at most the 40 newest. A single over-long row can therefore no longer push the others out.

What a hook injects is capped at 8,000 characters, so the rules and the corrections are injected by two separate hook commands, each with its own budget: the rules get one (the hook cuts the body at a line boundary and says so when `rules/routing.md` is longer, keeping the feedback-file line), and the corrections get another, the most recent rows kept when they do not all fit. Once a correction has proven itself, fold it into the rules and delete the row.

## Optimising another machine

`docs/optimize-a-machine.md` is a playbook for a Claude Code session on another machine: measure how Claude Code is used there, set the two settings, install this plugin (from a copied folder if the machine cannot reach the repository), prune unused skills and plugins, and make expensive commands run in a fresh context. `tools/session_report.py` is the read-only measurement script it uses: `python3 tools/session_report.py ~/.claude [other config dirs]`. `tools/before_after.py` compares the sessions that had the routing rules with the ones before: `python3 tools/before_after.py ~/.claude [other config dirs]`. Both count prompts with pasted content and slash commands, leave compaction summaries out (and count them) and price 1-hour cache writes at their list ratio; `before_after.py` also counts foreground agent results as reports and ignores background-launch acknowledgements. The playbook's appendix holds a copy of `session_report.py` for machines that cannot reach the plugin folder; a test keeps the copy identical.

## Tests

```
python3 -m unittest discover -s tests -v
```

`tests/test_hooks.py` runs every hook as a subprocess with a JSON payload and a temporary config directory, the way Claude Code runs it, with `MODEL_ROUTER_DEBUG=1` so a crash fails the test instead of passing as a silent no-op: the injected rules fit the cap and keep the feedback line even when `rules/routing.md` is far too long; corrections with and without a header are read; the model hook only ever injects an accepted alias and denies forks; the context watch warns once per size and stays quiet after a compaction; the nudge counts a batch once; the report budget lets a `REPORT FOR USER` through and saves everything else owner-only; the write guard denies and allows what it should; the two operator files share their gate and tool rules; the playbook appendix matches the tool; the versions agree. `tests/test_setup.py` runs the setup script's commands against a fake `claude` on `PATH`, and `tests/test_tools.py` runs the two measurement scripts on a synthetic transcript. The routing itself is measured separately: `claude plugin eval . --runs 1 --max-cost-usd 2` runs the twenty cases in `evals/` through a fresh session each and grades the transcript with a haiku judge; run it before and after a rules change. A GitHub Actions workflow runs them on Linux and Windows with Python 3.9 and 3.12. Hook behaviour was verified against Claude Code 2.1.286.

## Troubleshooting

- **Nothing is delegated.** Check `python3 --version` first; without Python (3.9+) the hooks do nothing. Then run `models.py doctor`: if the rules length is 0 or the rules file is missing, the plugin root is wrong (reinstall); if the plugin is disabled in a settings file, enable it; if `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set, every agent runs on that model and the tiers mean nothing. A session that was open when you installed gets the rules on its next prompt.
- **An operator was asked to confirm something you already approved.** The main session has to put `CONFIRMED BY USER` in the task; say "yes, run it" and it will. If it keeps happening for one kind of work, record a correction.
- **A skill's report came back as a summary with a file path.** The operator did not start it with `REPORT FOR USER`, or it was over 16,000 characters; the full text is in the file the path names.
- **An operator says a hook denied a command.** It tried to write a file outside the scratchpad; that is the write guard doing its job. Ask for the change as Building work, or tell the operator the scratchpad path to save output under.
- **The same rules appear twice in a resumed session.** Claude Code drops a SessionStart context that is already in the conversation; two copies mean the rules or the tier models changed in between, and the rules say the latest copy wins.

## Requirements

- Claude Code 2.1.286 or later: the hooks use the `PostToolBatch` event and the `omitClaudeMd` and `maxTurns` agent fields, and the rules budget assumes the 8,000-character cap of that version. On a version without `PostToolBatch` that entry is dropped (the loader ignores an unknown event name and keeps the others), so the delegation nudge never fires; without `omitClaudeMd` and `maxTurns` the operators load the CLAUDE.md files and have no turn cap. Older versions were not tested.
- At least one model available for subagents. The defaults are sonnet and haiku; `/model-router:setup` checks them and lets you choose others.
- Install it in every config directory you use — each `CLAUDE_CONFIG_DIR` has its own plugin list.
- `python3` or `python` (3.9 or later) on `PATH` for the hooks and the setup script.
