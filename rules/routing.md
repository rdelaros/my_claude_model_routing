## Model routing

Every tool call made here is billed against the whole conversation. Keep this session for thinking and talking with the user; push tool-heavy work into subagents, which run a cheaper model over a small fresh context and return a short result.

| Tier | Runs on | What |
|---|---|---|
| **Thinking** | Main session, never delegated | Design, trade-offs, reviews, explanations, deciding, discussing results. |
| **Building** | `model-router:builder`, model `{builder_model}` | Executing an agreed change: edits, new files, tests, commits. |
| **Operational** | `model-router:operator`, model `{operator_model}` | Running and reporting: git, PRs, pipelines, deployments, jobs, queries, group checks, Jira/Confluence updates. |

### Classify the work, not the wording

- "yes", "ok", "si", "continue" → the tier of whatever was just proposed.
- A bare URL, ticket key, PR or pipeline reference → Operational: fetch it, then discuss it here.
- Will edit files → Building. Will run commands or MCP calls and report → Operational. Can answer from what you know → Thinking.
- Typical Operational: pull, branch/rebase/checkout, "tag to promote", "is there a PR", "PR comments", "pipeline completes?", "check my groups", "update the Jira tickets". Building: "fix", "add", "rename", an approved plan. Thinking: "why", "should we", "review this".
- Mixed ("rebase and check what was implemented") → delegate the operational part, reason about the result here.

### When to delegate

1. Delegate when you expect **3+ tool calls**, **verbose output** (logs, diffs, query results), or **a wait** (pull, pipeline, job, deployment). One or two instant calls with short output: run them here.
2. Agree ticket, PR, and commit text with the user here first, then give the operator the final text verbatim.
3. Approved plan → independent work packages (no shared files or ordering), one builder each in parallel, each told its files and scoped tests. Always delegate changes touching 3+ files. Then integrate and run the full suite once.
4. The operator runs in the background. Launch it, tell the user in one line what is running, and end the turn or carry on — never wait idle or poll. Report when its completion notice arrives; never guess a result. Independent tasks go out in parallel. Give it everything up front (repo, branch, org/project, profile, environment, ticket keys, polling interval) — it cannot ask.
5. Pass the tier's `model` from the table on every `Agent()` call — the agent's built-in default may not exist on this provider (`/model-router:setup` changes it). In doubt between two tiers, choose the cheaper; if genuinely ambiguous, ask one short question.

### Confirmation gate

Before delegating any of these, state the exact command and target and get a yes — unless the user's own message already named both unambiguously:

- Anything targeting production, including tags or PR completions that promote to pre/prod.
- SQL that writes or destroys (INSERT, UPDATE, DELETE, MERGE, TRUNCATE, DROP, ALTER, GRANT, REVOKE).
- Cancelling or deleting jobs, pipelines, branches, tickets, or remote files.

Once confirmed, put the line `CONFIRMED BY USER` in the operator's task; it refuses gated actions without it. Never add it unasked.

### Corrections

When the user corrects a routing decision, append a row (date, situation, routed to, should be, why) to the feedback file below (create it with a table header if missing). Its rows override everything above.
