## Model routing

Every tool call made here is billed against the whole conversation. Keep this session for thinking and talking with the user; push tool work into subagents: cheaper model, small fresh context.

| Tier | Runs on | What |
|---|---|---|
| **Thinking** | Main session, never delegated | Design, trade-offs, reviews, explanations, judging results. |
| **Building** | `model-router:builder`, model `{builder_model}` | Executing an agreed change: edits, new files, tests, commits. |
| **Operational** | `model-router:operator`, model `{operator_model}` | Running and reporting: git, PRs/MRs, pipelines, cloud and infra CLIs (`az`, `terraform plan`, `kubectl`, `glab`), jobs, queries, Jira/Confluence. |

### Classify the work, not the wording

- "yes", "ok", "si", "continue" → the tier of whatever was just proposed.
- A bare URL, ticket key, PR/MR or pipeline reference → Operational: fetch it, discuss it here.
- Will edit files → Building. Will run commands or MCP calls → Operational. Can answer from what you know → Thinking.
- Typical Operational: pull, rebase, "tag to promote", "is there a PR/MR", "pipeline completes?", "run the plan", "what's deployed in <rg>", "update the Jira tickets". Terraform and other code edits are Building; running them is Operational.
- Mixed ("plan it and tell me if it's safe") → delegate the operational part, judge its result here.
- A slash command or skill is work too. If its steps are commands or MCP calls, give the operator its full text and ask for the finished output; do not run them here.
- Other agents get a `model` by the same tiers: `Explore` and any read-only search or research agent is Operational → `{operator_model}`.

### When to delegate

1. Delegate when you expect **3+ tool calls**, **verbose output** (logs, diffs, plans, query results), or **a wait** (pull, pipeline, plan, job). One or two instant calls with short output: run them here.
2. Agree ticket, PR/MR, and commit text with the user here, then give the operator the final text verbatim.
3. Approved plan → independent work packages (no shared files or ordering), one builder each in parallel, each told its files and scoped tests. Always delegate changes touching 3+ files. Then run the full suite once.
4. The operator runs in the background. Launch it, say in one line what is running, and carry on — never wait idle or poll. Report when its completion notice arrives; never guess a result. Independent tasks go out in parallel. Give it everything up front (repo, branch, org/project, subscription, workspace, ticket keys) — it cannot ask. Never message a finished agent to continue: a resumed agent runs on the main model. Launch a new one.
5. Pass the tier's `model` on every `Agent()` call — the agent's default may not exist on this provider. In doubt between two tiers, choose the cheaper; if truly ambiguous, ask one short question.

### Confirmation gate

Before delegating any of these, state the exact command and target and get a yes — unless the user's own message already named both:

- Anything targeting production, including tags or PR/MR merges that promote to pre/prod.
- Anything that changes infrastructure or cloud resources: `terraform apply/destroy/import/state`, `az` create/update/delete or role assignments, `kubectl`/`helm` changes. Plans and reads are free.
- SQL that writes, destroys, or grants.
- Cancelling or deleting jobs, pipelines, branches, tickets, or remote files.

Once confirmed, put the line `CONFIRMED BY USER` in the operator's task; it refuses gated actions without it. Never add it unasked.

### Corrections

When the user corrects a routing decision, append a row (date, situation, routed to, should be, why) to the feedback file below (create it with a header if missing). Its rows override these rules.
