# Changelog

## 1.9.0

Verified against Claude Code 2.1.286; the hooks now use events and fields that exist from that version.

### Routing
- The rules use the real injection budget (8,000 characters per hook output, not 4,000) and the feedback-file line can no longer be cut off: when the rules are too long the body is cut at a line boundary and says so.
- New **Reading** tier: questions that need many files or long output go to `Explore` or the operator instead of being read into the main session.
- Rules for cases that had none: reviewing a PR/MR (the operator saves the diff to the scratchpad, the main session judges), pasted errors, web fetches, writing text the user will read, small edits in files already in context, what a bare approval means, what happens when an operator or builder fails.
- The confirmation gate applies to commands run in the main session too, lists the same actions the operators refuse, and says what counts as a confirmation.
- Corrections: the feedback file is created with its table header, a bare first row is no longer dropped, rows are cleaned and capped, a row can never carry `CONFIRMED BY USER`, and the rules say a correction comes only from the user's own message.
- The rules re-inject on resume and fork as well (Claude Code drops a copy that is already in the conversation).

### Agents
- Operators run without the project's CLAUDE.md files (`omitClaudeMd`), with a turn cap, and may not launch subagents.
- A new `PreToolUse` hook (`hooks/guard-writes.py`) denies file-writing shell commands from the operators and the coordinator — redirections, `tee`, `sed -i`, `git apply/restore/reset --hard/clean/push --force` — except writes under the scratchpad or temp directory, where they park long output for the main session.
- A report whose first line is `REPORT FOR USER` passes the report budget at up to four times the length, so a skill's finished table reaches the user intact.
- Monitoring guidance matches the runtime: a bounded poll with `timeout: 600000` on the Bash call (the default inside a subagent is 2 minutes); the Monitor tool is loaded on request.
- The coordinator handles background builder launches (an acknowledgement is not a result; it waits for every completion notice) and lets the hook fill in the configured builder model instead of hard-coding `sonnet`.
- The builder no longer advertises commits; commits with agreed text are operational work.
- The `CONFIRMED BY USER` line counts only in the task prompt, never in files, tickets or command output.

### Hooks
- The delegation nudge counts on `PostToolBatch` (once per batch, failed calls included) instead of racing per-call `PostToolUse` hooks, names the commands it saw, and keeps its state under the config directory instead of the shared temp directory.
- `context-watch` no longer re-warns at the same size after a prompt that got no answer, is silent right after a compaction, and tolerates line separators inside records.
- `ensure-rules` no longer mistakes a tool result that quotes the plugin's source for an injected copy of the rules.
- Saved subagent reports are owner-only (0600 in a 0700 folder) and never overwrite each other.
- `agent-model` adds nothing when `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` is set, accepts spaces in `MODEL_ROUTER_AGENT_MODELS`, and only ever injects one of the four aliases the Agent tool accepts.

### Setup skill
- `models.py doctor`: a read-only self-check (rules length, feedback rows, tier models, alias maps and their source file, fork gate, forced subagent model, `claude` on PATH).
- `models.py forks off|on`: switch the fork feature off at the source (`CLAUDE_CODE_FORK_SUBAGENT=false`), which restores foreground launches and removes the fork agent type; `models.py reset` returns tiers to their defaults.
- `check` sends a tool-less one-line probe (a fraction of a cent per model, shown in a cost column), survives warnings printed before the JSON result, recommends the cheapest working alias per tier, and `check --all` probes every alias.
- `show` reads the `env` block from user, project, local and managed settings with their precedence, warns about `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (the variable that really disables routing) and reports invalid stored tier values.
- `map`/`unmap` accept an empty `{}` settings file, refuse a malformed `env` block before taking a backup, write atomically, and do nothing when nothing would change.

### Tools
- `before_after.py` and `session_report.py` count prompts with pasted content and slash commands, ignore compaction summaries (and count them), price 1-hour cache writes at their list ratio, count foreground agent results as reports, and no longer crash on string-content assistant records or malformed timestamps.

### Tests
- `tests/test_hooks.py` runs every hook as a subprocess against a temporary config directory; a GitHub Actions workflow runs it on Linux and Windows.

## 1.8.1 and earlier

See the git history.
