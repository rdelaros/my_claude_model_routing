## Model routing

Every tool call here is billed against the whole conversation. Keep this session for thinking; push tool work to subagents.

| Tier | Runs on | What |
|---|---|---|
| **Thinking** | Main session, never delegated | Design, trade-offs, reviews, explanations, judging results. |
| **Building** | `model-router:builder`, model `{builder_model}`; big plans: `model-router:coordinator` | Executing an agreed change: edits, new files, tests, commits. |
| **Operational** | `model-router:operator`, model `{operator_model}` | Known commands: git, PRs/MRs, pipelines, `az`, `terraform plan`, `kubectl`, `glab`, jobs, queries, Jira/Confluence. |
| **Diagnosis** | `model-router:senior-operator`, model `{senior_operator_model}` | Operational work needing judgement: why a pipeline, plan or job failed, multi-step investigations, conflicts, anything the operator got wrong. |

### Classify the work, not the wording

- "yes", "ok", "si", "continue" → the tier of what was just proposed.
- A bare URL, ticket key, PR/MR or pipeline reference → Operational: fetch it, discuss it here.
- Will edit files → Building. Will run commands or MCP calls → Operational. Answerable from what you know → Thinking.
- "tag to promote", "is there a PR/MR", "pipeline completes?", "run the plan" → Operational. Editing Terraform or code is Building; running it is Operational.
- Mixed ("plan it and tell me if it's safe") → delegate the operational part, judge its result here.
- A slash command or skill is work too. If its steps are commands or MCP calls, give the operator its full text and ask for the finished output.
- Other agents get a `model` by the same tiers; `Explore` or any read-only search agent → `{operator_model}`.

### When to delegate

1. Delegate when you expect **3+ tool calls**, **verbose output** (logs, diffs, plans, query results), or **a wait** (pull, pipeline, job). One or two instant calls with short output: run them here.
2. Agree ticket, PR/MR, and commit text with the user here, then give the operator the final text verbatim.
3. Approved plan of one or two files → one builder, told its files and scoped tests. 3+ files or modules, or ordered parts → give the plan, repo path, test command and builder model to `model-router:coordinator`; it splits, runs builders in parallel, tests and reports once. Never write 3+ files here.
4. Operators and coordinator run in the background. Launch, say in one line what is running, and carry on — never wait idle or poll. Report when the completion notice arrives; never guess. Independent tasks go out in parallel. Give everything up front (repo, branch, project, subscription, ticket keys); they cannot ask. Message a finished agent to continue only when its context is needed; otherwise launch a new one.
5. Pass the tier's `model` on every `Agent()` call; the agent's default may not exist on this provider. A failed cheap-tier agent is retried one tier up, not redone here. In doubt, choose the cheaper tier.
6. Never use `subagent_type: "fork"` for tool work: it copies the whole conversation and runs on the main model. Use operator, senior-operator, Explore or builder.

### Confirmation gate

Before delegating anything that touches production (including tags or merges that promote to pre/prod), changes infrastructure (`terraform apply/destroy/import/state`, `az` create/update/delete or role assignments, `kubectl`/`helm` changes), writes or grants in SQL, or cancels or deletes jobs, pipelines, branches, tickets or remote files: state the exact command and target and get a yes, unless the user's message already named both. Plans and reads are free.

Once confirmed, put the line `CONFIRMED BY USER` in the operator's task; it refuses gated actions without it. Never add it unasked.

### Corrections

When the user corrects a routing decision, append a row (date, situation, routed to, should be, why) to the feedback file below (create it if missing). Its rows override these rules.
