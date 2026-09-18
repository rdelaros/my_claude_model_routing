#!/usr/bin/env python3
"""model-router setup helper: show, check, and set the models behind each routing tier.

  models.py show                       current tier models, alias mappings, anything overriding them
  models.py check [model ...]          call each model once through `claude -p` and report who answered
  models.py set builder=<alias> operator=<alias> senior_operator=<alias>
  models.py map <alias>=<model-id>     point an alias at a model id / deployment name (writes settings.json env)
  models.py unmap <alias>

Everything is per config directory (CLAUDE_CONFIG_DIR, default ~/.claude), because different
configs can sit behind different providers. `check` runs in the environment it is called from,
so run it from inside the session whose gateway you want to test.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ALIASES = ("haiku", "sonnet", "opus", "fable")  # what the Agent tool's `model` parameter accepts
TIERS = {"builder": "sonnet", "operator": "haiku", "senior_operator": "sonnet"}
ALIAS_ENV = {a: f"ANTHROPIC_DEFAULT_{a.upper()}_MODEL" for a in ALIASES}
CHECK_TIMEOUT = 120


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def router_config_path():
    return config_dir() / "model-router" / "config.json"


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def tier_models():
    saved = load_json(router_config_path())
    return {tier: saved.get(tier) if saved.get(tier) in ALIASES else default for tier, default in TIERS.items()}


def settings_env():
    return load_json(config_dir() / "settings.json").get("env") or {}


def effective_env(name):
    """Process env wins over settings.json env, as it does in Claude Code."""
    return os.environ.get(name) or settings_env().get(name)


def provider():
    for flag, label in (("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"), ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
                        ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI")):
        if effective_env(flag):
            return label
    return "Anthropic API gateway" if effective_env("ANTHROPIC_BASE_URL") else "Anthropic (direct)"


def cmd_show(_args):
    print(f"config dir : {config_dir()}")
    print(f"provider   : {provider()}")
    print(f"main model : {load_json(config_dir() / 'settings.json').get('model') or '(default)'}")
    print("tiers      :")
    for tier, model in tier_models().items():
        print(f"  {tier:15} -> {model}{'' if load_json(router_config_path()).get(tier) else '  (default)'}")
    print("alias maps :")
    mapped = {alias: effective_env(var) for alias, var in ALIAS_ENV.items() if effective_env(var)}
    for alias, value in mapped.items():
        print(f"  {alias:9} -> {value}   ({ALIAS_ENV[alias]})")
    if not mapped:
        print("  (none — each alias resolves to Claude Code's default model id)")
    forced = effective_env("CLAUDE_CODE_SUBAGENT_MODEL")
    if forced:
        print(f"WARNING    : CLAUDE_CODE_SUBAGENT_MODEL={forced} forces every subagent onto that model; tier routing has no effect until it is unset.")
    return 0


def family(model_id):
    return next((a for a in ALIASES if a in model_id.lower()), None)


def check_one(model):
    started = time.time()
    try:
        proc = subprocess.run(
            ["claude", "-p", "Reply with the single word OK.", "--model", model, "--output-format", "json", "--max-turns", "1"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CHECK_TIMEOUT)
    except FileNotFoundError:
        return {"model": model, "status": "FAIL", "detail": "`claude` not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"model": model, "status": "FAIL", "detail": f"no answer within {CHECK_TIMEOUT}s"}
    seconds = time.time() - started

    try:
        data = json.loads(proc.stdout)
        result = data[-1] if isinstance(data, list) else data
    except ValueError:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"model": model, "status": "FAIL", "detail": (tail[-1] if tail else f"exit code {proc.returncode}")[:200]}

    if result.get("is_error") or result.get("subtype") != "success":
        detail = str(result.get("result") or result.get("subtype") or "error")
        if result.get("api_error_status"):
            detail = f"HTTP {result['api_error_status']}: {detail}"
        return {"model": model, "status": "FAIL", "detail": detail[:200]}

    # Claude Code also makes small helper calls on its fast model; the model that answered is
    # the one that carried the conversation context, not the one with the most output tokens.
    def context_tokens(usage):
        return sum(usage.get(k) or 0 for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))

    answered = sorted((result.get("modelUsage") or {}).items(), key=lambda kv: -context_tokens(kv[1]))
    answered_by = answered[0][0] if answered else "?"
    out = {"model": model, "status": "OK", "answered_by": answered_by, "seconds": round(seconds, 1),
           "cost_usd": result.get("total_cost_usd")}
    wanted = family(effective_env(ALIAS_ENV[model]) or model) if model in ALIASES else family(model)
    if wanted and family(answered_by) and family(answered_by) != wanted:
        out["status"] = "FALLBACK"
        out["detail"] = f"asked for {wanted}, but {answered_by} answered"
    return out


def cmd_check(args):
    models = args or list(dict.fromkeys(list(tier_models().values()) + ["haiku", "sonnet"]))
    print(f"provider: {provider()}   config dir: {config_dir()}")
    print(f"checking {', '.join(models)} — one minimal request each\n")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check_one, models))
    print(f"{'requested':28} {'status':9} {'answered by':34} {'time':>6}  detail")
    for r in results:
        print(f"{r['model']:28} {r['status']:9} {r.get('answered_by', '-'):34} {str(r.get('seconds', '-')) + 's':>6}  {r.get('detail', '')}")
    print("\n" + json.dumps({"results": results}))
    return 0 if all(r["status"] == "OK" for r in results) else 1


def cmd_set(args):
    chosen = {}
    for arg in args:
        tier, _, model = arg.partition("=")
        if tier not in TIERS or model not in ALIASES:
            print(f"invalid: {arg!r} — use <tier>=<alias>, tier one of {', '.join(TIERS)}, alias one of {', '.join(ALIASES)}", file=sys.stderr)
            return 2
        chosen[tier] = model
    if not chosen:
        print("nothing to set", file=sys.stderr)
        return 2
    path = router_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = load_json(path)
    saved.update(chosen)
    path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    print(f"saved {chosen} -> {path}\nTakes effect in sessions started from now on.")
    return 0


def write_alias_map(alias, value):
    if alias not in ALIASES:
        print(f"unknown alias {alias!r}; one of {', '.join(ALIASES)}", file=sys.stderr)
        return 2
    path = config_dir() / "settings.json"
    settings = load_json(path)
    if path.exists() and not settings:
        print(f"{path} exists but could not be parsed; not touching it", file=sys.stderr)
        return 1
    if path.exists():
        backup = path.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}")
    env = settings.setdefault("env", {})
    if value is None:
        env.pop(ALIAS_ENV[alias], None)
        if not env:
            settings.pop("env")
    else:
        env[ALIAS_ENV[alias]] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{ALIAS_ENV[alias]} {'removed from' if value is None else '= ' + value + ' in'} {path}\nTakes effect in sessions started from now on.")
    if os.environ.get(ALIAS_ENV[alias]):
        print(f"NOTE: {ALIAS_ENV[alias]} is also set in the shell environment, which overrides settings.json.")
    return 0


def cmd_map(args):
    if len(args) != 1 or "=" not in args[0]:
        print("usage: models.py map <alias>=<model-id>", file=sys.stderr)
        return 2
    alias, _, value = args[0].partition("=")
    return write_alias_map(alias, value.strip() or None)


def cmd_unmap(args):
    if len(args) != 1:
        print("usage: models.py unmap <alias>", file=sys.stderr)
        return 2
    return write_alias_map(args[0], None)


COMMANDS = {"show": cmd_show, "check": cmd_check, "set": cmd_set, "map": cmd_map, "unmap": cmd_unmap}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
