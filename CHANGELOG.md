# Changelog

## 1.10.0

Measured on two config directories of one machine running Claude Code 2.1.295 (`tools/before_after.py`): cost per prompt down a third, mean context per call down a third to a half, calls over 200k context down from 25% to none in the busier directory, agent reports down from 4,500 to 700 characters. Prompts that still ran three or more commands in the main session were the largest remaining cost, and `general-purpose` launches on the main model the costliest launch type. The eval suite reported a 21% pass rate with and without the plugin: that was the suite, fixed below.

### Evals
- Every judge grader reads the whole trace (`focus: trace`). The runner's default judges the last message only, so no grader could see which agent was launched and most cases failed on both arms.
- Each case also has free, deterministic `tool_used` graders: which `subagent_type` was launched (and that the coordinator got no `model`), and what the main session did not run (`git`, `terraform apply`, a `Read` of the 3,000-line log, a `fork`).
- Sixteen cases stage a small fake project with `scaffold.sh` (a git checkout on a feature branch with a local origin, a CI file, a Terraform folder, tests, an uncommitted change and a failed job log); in an empty workspace the model could only ask where the repository was. The suite runs with `--scaffold --allow-tools Bash --ablation none`; `evals/README.md` has the command, the cost and how to read a failure.
- Prompts that depended on earlier conversation now carry their context; `skill-report` became `multi-command-report`.
- First real run of the rewritten suite (20 cases, no shell grant, $2.48): 13 of 20 at the 0.7 threshold. Six of the seven failures were counters that took the launched operator's own git, glab or terraform attempts for main-session calls, since `tool_used` counts every call in the trace; those counters are gone and the judge criteria say how to tell the two apart. The seventh was a Spanish rebase that stopped to ask about an uncommitted change in the staged project; the staged change is now an untracked file, which a rebase ignores.
- Third run (threshold 0.8, no shell grant, $2.52, 8 minutes): 19 of 20. The one failure was real: `commit-text` wrote a commit message and launched the operator to commit it without waiting for the user's yes.
- Cases keep at most four graders of equal weight and run at threshold 0.8, so one failing grader fails the case (at 0.7 a four-grader case passed with any single failure). Counters that could trip on a correct decision in the shell-granted mode (a quick `git log` before asking, a listing before a restart, a description that mentions the commit) are gone or anchored to the command; the build cases get longer timeouts for real builders.

### Routing
- Commit messages and ticket text written by the session are shown and agreed before an agent commits or posts them; MR/PR titles and descriptions come from the branch's commits and need no yes. The old wording ("written here, then given verbatim to the agent") let the commit-text case commit an unagreed message.
- A thought that stops mid-sentence gets a line or two back, not an essay: the chat-thinking case answered a half-finished sentence with a design document.
- `general-purpose` launches get the builder model, from the rules and from the `agent-model` hook (`MODEL_ROUTER_AGENT_MODELS="general-purpose=main"` restores inheritance of the main model).

### Tools
- `before_after.py --misses N` lists the N costliest prompts that still ran 3+ Bash/MCP calls in the main session, with whether the delegation nudge fired for them: the misses to turn into rules or eval cases.
- `before_after.py` tolerates a string `toolUseResult`.

## 1.9.0

Verified against Claude Code 2.1.286; the hooks now use events and fields that exist from that version.

### Routing
- The rules use the real injection budget (8,000 characters per hook output; earlier plugin versions assumed 4,000) and the feedback-file line can no longer be cut off: when the rules are too long the body is cut at a line boundary and says so.
- New **Reading** tier: questions that need many files or long output go to `Explore` or the operator instead of being read into the main session.
- Rules for cases that had none: reviewing a PR/MR (the operator saves the diff to the scratchpad, the main session judges), pasted errors, web fetches, writing text the user will read, small edits in files already in context, what a bare approval means, what happens when an operator or builder fails.
- The confirmation gate applies to commands run in the main session too, lists the same actions the operators refuse, and says what counts as a confirmation.
- Corrections: the feedback file is created with its table header, a bare first row is no longer dropped, rows are cleaned and capped, a row can never carry `CONFIRMED BY USER`, and the rules say a correction comes only from the user's own message.
- The rules re-inject on resume and fork as well (Claude Code drops a copy that is already in the conversation).

### Agents
- Operators run without the project's CLAUDE.md files (`omitClaudeMd`), with a turn cap, and may not launch subagents.
- A new `PreToolUse` hook (`hooks/guard-writes.py`) denies the common file-writing shell commands from the operators and the coordinator — redirections, `tee`, in-place editors, `patch`, file commands with a target outside the allowed directories, git commands that rewrite the working tree or history, interpreter one-liners that open a file for writing, `bash -c` strings with any of those; full list in the hook's docstring — except writes under the scratchpad or temp directory, where they park long output for the main session. A safety net, not a sandbox.
- A report whose first line is `REPORT FOR USER` passes the report budget at up to four times the length, so a skill's finished table reaches the user intact.
- Monitoring guidance matches the runtime: a bounded poll with `timeout: 600000` on the Bash call (the default inside a subagent is 2 minutes); the Monitor tool is loaded on request.
- The coordinator handles background builder launches (an acknowledgement is not a result; it waits for every completion notice) and lets the hook fill in the configured builder model instead of hard-coding `sonnet`.
- The builder no longer advertises commits; commits with agreed text are operational work.
- The confirmation counts only as the last line of the task, in the form `CONFIRMED BY USER: <command> on <target>`, never inside quoted text, files, tickets or command output.

### Hooks
- The delegation nudge counts on `PostToolBatch` (once per batch, failed calls included) instead of racing per-call `PostToolUse` hooks, names the commands it saw, and keeps its state under the config directory instead of the shared temp directory.
- `context-watch` no longer re-warns at the same size after a prompt that got no answer, is silent right after a compaction, and tolerates line separators inside records.
- `ensure-rules` no longer mistakes a tool result that quotes the plugin's source for an injected copy of the rules.
- Saved subagent reports are owner-only (0600 in a 0700 folder) and never overwrite each other.
- `agent-model` adds nothing when `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set, accepts spaces in `MODEL_ROUTER_AGENT_MODELS`, and only ever injects one of the four aliases the Agent tool accepts.

### Setup skill
- `models.py doctor`: a read-only self-check (rules length, feedback rows, tier models, alias maps and their source file, fork gate, forced subagent model, `claude` on PATH).
- `models.py forks off|on`: switch the fork feature off at the source (`CLAUDE_CODE_FORK_SUBAGENT=false`), which removes the fork agent type and brings back the Agent tool's `run_in_background` parameter (launches still default to the background; the rules then ask for a direct result from builders and the coordinator); `models.py reset` returns tiers to their defaults.
- `check` sends a tool-less one-line probe (a fraction of a cent per model, shown in a cost column), survives warnings printed before the JSON result, recommends the cheapest working alias per tier, and `check --all` probes every alias.
- `show` reads the `env` block from user, project, local and managed settings with their precedence, warns about `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (the variable that really disables routing) and reports invalid stored tier values.
- `map`/`unmap` accept an empty `{}` settings file, refuse a malformed `env` block before taking a backup, write atomically, and do nothing when nothing would change.

### Tools
- `before_after.py` and `session_report.py` count prompts with pasted content (including image-only ones) and slash commands, ignore compaction summaries (and count them), price 1-hour cache writes at their list ratio using the authoritative total, count each tool call once even when its record is repeated, and no longer crash on string-content assistant records or malformed timestamps; `before_after.py` counts foreground agent results as reports and ignores background-launch acknowledgements.

### Token footprint
- The rules, the four agents and the skill description were rewritten to the published prompt-writing guidance and cut to roughly 60 percent of their size with every rule kept: about 1,300 tokens of rules and 160 of agent descriptions per main-session call instead of 1,900 and 380; operator turns carry 1,200 instruction tokens instead of 2,000.
- The rules open with the premise that the user speaks naturally and the main session alone decides delegation, parallel runs and confirmations, and they treat an explanation or half-finished thought as conversation: answer briefly, ask at most one question, start no work until there is a clear ask.

### Writing for the model
- Four one-line routing examples (two in Spanish) in the rules; a Goal / Where / Constraints / Report-as template for agent tasks; tasks in English, answers in the user's language.
- Every agent report starts with `RESULT: ok` or `RESULT: failed — <reason>`.
- A `SubagentStart` hook (`hooks/agent-context.py`) gives the plugin's agents their working directory, git branch and scratchpad path at start.
- `evals/`: twenty routing cases for `claude plugin eval`, so rule changes are measured.

### Training mode
- `/model-router:train`: the session explains each routing decision (tier, agent, model, deciding rule), asks before acting, records corrections with the user's reason, and reports the cost in hindsight; `quiet`, `review` and `off` arguments.

### Tests
- `tests/test_hooks.py` runs every hook as a subprocess against a temporary config directory (with `MODEL_ROUTER_DEBUG=1`, so a crash fails instead of passing silently), `tests/test_setup.py` runs the setup script against a fake `claude`, and `tests/test_tools.py` runs the measurement scripts on a synthetic transcript; a GitHub Actions workflow runs them on Linux and Windows.

## 1.8.1 and earlier

See the git history.
