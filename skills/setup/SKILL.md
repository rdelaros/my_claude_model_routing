---
name: setup
description: Set up which model the model-router plugin uses for its Building, Operational and Diagnosis tiers, verify that each model actually answers on this provider, and check that the router is active. Use when the user wants to configure or change the router's models, when a builder or operator subagent fails with a model error, after installing the plugin under a new config directory or gateway (Bedrock, Vertex, Foundry), when asked whether sonnet/haiku work here, or when the user asks "is the router working", "why was nothing delegated", or for the model-router doctor.
---

# model-router setup

Not every provider serves every model. A gateway may have no haiku deployment, or may serve it under its own deployment name. This skill finds out what works **in this session's environment** and points each tier at a model that does.

All commands below use the helper script in this skill's directory: `scripts/models.py`, relative to the base directory shown when this skill loaded. Run it with `python3` (or `python`). Settings are per config directory (`CLAUDE_CONFIG_DIR`), so a user with several configs runs this once inside each.

## 1. Look

```
python3 <skill dir>/scripts/models.py show
python3 <skill dir>/scripts/models.py doctor
```

`show` prints the provider, the main model, the alias each tier uses (a stored value that is not an alias is flagged `INVALID` and the default is used), any alias mapped to a specific model id with the settings file that sets it, and the fork gate. If it prints a `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` warning, tell the user first: with that variable the Agent tool ignores every `model`, so nothing below has any effect until it is removed. The plain `CLAUDE_CODE_SUBAGENT_MODEL` is only a note: it is the default for agents that name no model, and the router's agents all do.

`doctor` is read-only and free. It checks that the files are in place (plugin root, rules file, hooks importable), what would be injected (the length of the rules and corrections text, the feedback file and its rows, session markers), whether the plugin is enabled in settings (`plugin enabled : model-router@<marketplace> = true|false (<file>)`; `false` is a problem, a missing entry is only a note for a `--plugin-dir` checkout) and the overrides (tier models, mappings, fork gate, the subagent-model variables), plus the `claude` on PATH, ending with `problems: none` or a list (exit 1). It reads the settings files on disk, so a `--settings` file or JSON on the command line is invisible to it. Run it whenever the user wonders whether the router is active or why nothing was delegated, and read its problem list to them.

Both commands read the variables the way Claude Code does: a settings file beats the shell. A `note` line says when an exported shell value is overridden by a file, and a source of `exported by the running session (stale; restart to clear)` means this session still carries a value that no settings file sets any more.

## 2. Check

```
python3 <skill dir>/scripts/models.py check
```

Sends one minimal request per model (the tier aliases plus `haiku` and `sonnet`) through `claude -p`, in parallel, and reports for each: `OK`, `FAIL` with the provider's error, or `FALLBACK` when a different model family answered than the one asked for. The probe is a one-line prompt with no tools and a one-line system prompt, so it costs a fraction of a cent per model; the table has a cost column and a total. Tell the user you are about to make the requests. Allow about two minutes per group of four models.

When a tier's alias is not `OK`, run `check --all` (all four aliases) or `check opus fable` before recommending anything, so the summary can pick from models that are known to work. To test anything else, a full model id or a gateway deployment name, pass it: `models.py check my-haiku-deployment`.

Show the user the result table as it is. Do not summarise a FAIL as "unavailable" without the error text: a 404 means the model or deployment does not exist, a 401/403 is a credentials or permission problem, and a timeout says nothing about the model.

## 3. Decide, with the user

A tier's model must be one of the aliases the `Agent` tool accepts: `haiku`, `sonnet`, `opus`, `fable`. The per-tier summary under the table gives the recommendation: for each tier that is not `OK`, the cheapest alias at or above it (haiku < sonnet < opus < fable) that checked `OK`, and a ready-to-copy `set` line. Let the user choose; use AskUserQuestion when more than one option works.

- **Alias failed, but the provider has that model under another name**: ask the user for the model id or deployment name, `check` it, and if it answers, map the alias to it (step 4). Do not guess deployment names.
- **Nothing cheaper than the main model works**: still set the tiers to a working alias. Delegation keeps tool calls out of the main context, which is most of the saving even at the same price per token.
- **Operational tier on a model larger than haiku**: fine, only less cheap. The senior operator (diagnosis, multi-step operational work) should stay a tier above the operator; if only one model works, both use it.
- `FALLBACK` is not a pass. The tier would silently run on the other model; treat it like a FAIL for that alias and say which model answered.

## 4. Apply

Tier models (stored in `<config dir>/model-router/config.json`, read by the plugin's hooks):

```
python3 <skill dir>/scripts/models.py set builder=sonnet operator=haiku senior_operator=sonnet
python3 <skill dir>/scripts/models.py reset [tier ...]       # back to the defaults (all tiers when none named)
```

Alias mapping, only when the user gave you a model id that checked `OK`. This writes `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` into the `env` block of `<config dir>/settings.json`, the user's own settings file, so state exactly what will be written and get a yes first. The script backs the file up before writing and warns when a project, local or managed settings file already sets the same variable (those win; a value exported in the shell loses to the file and is only noted).

```
python3 <skill dir>/scripts/models.py map haiku=<model-id>
python3 <skill dir>/scripts/models.py unmap haiku
```

A mapping applies to every use of that alias in Claude Code, not only to this plugin.

## 5. Forks (optional)

With Claude Code's fork feature on (the default in 2.1.28x), every Agent launch is asynchronous and the model is told to "fork yourself" for side tasks, which the plugin's hook denies. `forks off` writes `CLAUDE_CODE_FORK_SUBAGENT=false` into the `env` block of `<config dir>/settings.json`: it removes the fork agent type (so there is nothing to deny) and puts the `run_in_background` parameter back on the Agent tool. Launches still run in the background by default; the routing rules then tell the main session to pass `run_in_background: false` for builders and the coordinator (operators keep `background: true` regardless). Offer it when `show` says the fork gate is on; say exactly what will be written and get a yes first. `forks on` removes the variable. A value that is not a recognised boolean is ignored by Claude Code (`show` says so, gate stays on).

```
python3 <skill dir>/scripts/models.py forks off
```

## 6. Confirm

Run `check` again for whatever was mapped and `doctor` once. A new mapping takes effect in sessions started from now on; `check` re-reads the settings files (inside a running session it drops the session's already-exported model variables from the probe's environment, so the probe sees the new value even though this session does not). Then tell the user, in two or three lines: which model each tier now uses, what was mapped, and that the change applies to sessions started from now on. If a tier could not be given a working model, say so plainly rather than leaving the default in place silently.
