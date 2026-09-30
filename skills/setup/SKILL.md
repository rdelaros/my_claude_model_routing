---
name: setup
description: Set up which model the model-router plugin uses for its Building and Operational tiers, and verify that each model actually answers on this provider. Use when the user wants to configure or change the router's models, when a builder or operator subagent fails with a model error, after installing the plugin under a new config directory or gateway (Bedrock, Vertex, Foundry), or when asked whether sonnet/haiku work here.
---

# model-router setup

Not every provider serves every model. A gateway may have no haiku deployment, or may serve it under its own deployment name. This skill finds out what works **in this session's environment** and points each tier at a model that does.

All commands below use the helper script in this skill's directory: `scripts/models.py`, relative to the base directory shown when this skill loaded. Run it with `python3` (or `python`). Settings are per config directory (`CLAUDE_CONFIG_DIR`), so a user with several configs runs this once inside each.

## 1. Look

```
python3 <skill dir>/scripts/models.py show
```

Shows the provider, the main model, the model alias each tier uses, and any alias mapped to a specific model id. If it prints a `CLAUDE_CODE_SUBAGENT_MODEL` warning, tell the user first: that variable forces every subagent onto one model, and nothing below has any effect until it is removed.

## 2. Check

```
python3 <skill dir>/scripts/models.py check
```

Sends one minimal request per model (the tier models plus `haiku` and `sonnet`) through `claude -p`, in parallel, and reports for each: `OK`, `FAIL` with the provider's error, or `FALLBACK` when a different model family answered than the one asked for. Each request costs a few cents at most; tell the user you are about to make them. Allow up to two minutes.

To test anything else — another alias, a full model id, a gateway deployment name — pass it: `models.py check opus my-haiku-deployment`.

Show the user the result table as it is. Do not summarise a FAIL as "unavailable" without the error text: a 404 means the model or deployment does not exist, a 401/403 is a credentials or permission problem, and a timeout says nothing about the model.

## 3. Decide, with the user

A tier's model must be one of the aliases the `Agent` tool accepts: `haiku`, `sonnet`, `opus`, `fable`. For each tier, recommend the cheapest alias that checked `OK` (haiku < sonnet < opus/fable), and let the user choose — use AskUserQuestion when more than one option works.

- **Alias failed, but the provider has that model under another name**: ask the user for the model id or deployment name, `check` it, and if it answers, map the alias to it (step 4). Do not guess deployment names.
- **Nothing cheaper than the main model works**: still set the tiers to a working alias. Delegation keeps tool calls out of the main context, which is most of the saving even at the same price per token.
- **Operational tier on a model larger than haiku**: fine, only less cheap.
- `FALLBACK` is not a pass. The tier would silently run on the other model; treat it like a FAIL for that alias and say which model answered.

## 4. Apply

Tier models (stored in `<config dir>/model-router/config.json`, read by the plugin's session hook):

```
python3 <skill dir>/scripts/models.py set builder=sonnet operator=haiku
```

Alias mapping, only when the user gave you a model id that checked `OK`. This writes `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` into the `env` block of `<config dir>/settings.json` — the user's own settings file — so state exactly what will be written and get a yes first. The script backs the file up before writing.

```
python3 <skill dir>/scripts/models.py map haiku=<model-id>
python3 <skill dir>/scripts/models.py unmap haiku
```

A mapping applies to every use of that alias in Claude Code, not only to this plugin.

## 5. Confirm

Run `check` again for whatever was mapped — a new mapping only takes effect in new processes, which the check is. Then tell the user, in two or three lines: which model each tier now uses, what was mapped, and that the change applies to sessions started from now on. If a tier could not be given a working model, say so plainly rather than leaving the default in place silently.
