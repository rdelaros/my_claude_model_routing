#!/usr/bin/env python3
"""model-router setup helper: show, check and set the models behind each routing tier; a doctor for the router.

  models.py show                        tier models, alias mappings (with the file that sets each), overrides
  models.py check [--all] [model ...]   one minimal request per model through `claude -p`: who answered, cost
  models.py set builder=<alias> operator=<alias> senior_operator=<alias>
  models.py reset [tier ...]            forget the stored tier models (all when none named) -> defaults
  models.py map <alias>=<model-id>      point an alias at a model id / deployment name (settings.json env)
  models.py unmap <alias>
  models.py forks off|on                off: CLAUDE_CODE_FORK_SUBAGENT=false in settings.json env; on: remove it
  models.py doctor                      read-only self-check: is the router installed, injected and active?

Everything is per config directory (CLAUDE_CONFIG_DIR, default ~/.claude), because different
configs can sit behind different providers. `check` runs in the environment it is called from,
so run it from inside the session whose gateway you want to test. It always probes the three
tier aliases; named models are probed as well and `--all` adds the remaining aliases. Its exit
code is 0 iff every tier's current alias answered OK -- the extra probes never change it.
`doctor` exits 1 when it found a problem. Writers exit 2 on a usage error, 1 when they refuse a file.

Settings precedence, lowest to highest, as Claude Code merges the `env` blocks: user settings
(<config dir>/settings.json), <cwd>/.claude/settings.json, <cwd>/.claude/settings.local.json,
managed settings (managed-settings.json, then managed-settings.d/*.json). Claude Code applies
each layer's env block over the launch environment, so a settings file beats the shell for the
variables handled here; the shell only counts when no settings file sets the variable. A
--settings file or JSON given on the command line sits between the local and the managed layer
and is not visible from here. `map`, `unmap` and `forks` write the user file only.

Inside a running session (the Bash tool sets CLAUDECODE=1) the settings env is already exported
into the process: `check` scrubs those variables from the child's environment so `claude -p`
re-reads the settings files, and `show`/`doctor` call an exported value that no settings file
sets any more stale.

Forks: in Claude Code 2.1.28x every Agent launch is asynchronous while the fork feature is on
(the Agent tool has no run_in_background parameter) and its own prompt tells the model to "fork
yourself" while this plugin's hook denies forks. `forks off` (CLAUDE_CODE_FORK_SUBAGENT=false)
removes the fork agent type (so there is nothing to deny) and puts the run_in_background
parameter back on the Agent tool; launches still run in the background by default, and the
routing rules then tell the main session to pass run_in_background: false for builders and the
coordinator (operators keep background: true regardless).
"""
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ALIASES = ("haiku", "sonnet", "opus", "fable")  # what the Agent tool's `model` parameter accepts, cheapest first
TIERS = {"builder": "sonnet", "operator": "haiku", "senior_operator": "sonnet"}
ALIAS_ENV = {a: f"ANTHROPIC_DEFAULT_{a.upper()}_MODEL" for a in ALIASES}
FORK_VAR, SUBAGENT_VAR, FORCE_VAR = "CLAUDE_CODE_FORK_SUBAGENT", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
STALE_SOURCE = "exported by the running session (stale; restart to clear)"
CHECK_TIMEOUT = 120  # per model; the pool runs CHECK_WORKERS at a time
CHECK_WORKERS = 4
PROBE_BUDGET_USD = "0.25"


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def router_dir():
    return config_dir() / "model-router"


def router_config_path():
    return router_dir() / "config.json"


def load_json(path, default=None):
    """Parsed JSON, or `default` when the file is missing, unreadable or not valid JSON."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def stored_tiers():
    saved = load_json(router_config_path(), {})
    return saved if isinstance(saved, dict) else {}


def tier_models():
    """Tier -> alias in effect: the stored value when it is a known alias, else the default (as the hooks do)."""
    saved = stored_tiers()
    return {tier: saved.get(tier) if saved.get(tier) in ALIASES else default for tier, default in TIERS.items()}


def invalid_tiers():
    """Tier -> stored value that is not one of the aliases (ignored by the hooks)."""
    return {tier: value for tier, value in stored_tiers().items() if tier in TIERS and value not in ALIASES}


# --- settings files, merged the way Claude Code merges them ---------------------------------

def managed_settings_base():
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if sys.platform.startswith("win"):
        return Path(r"C:\Program Files\ClaudeCode")  # hardcoded in Claude Code; it does not consult %ProgramFiles%
    return Path("/etc/claude-code")


def managed_settings_paths():
    """managed-settings.json, then the managed-settings.d/*.json drop-ins (regular files, by name) -- on every platform."""
    base = managed_settings_base()
    try:
        drop_ins = sorted(e.name for e in os.scandir(base / "managed-settings.d") if e.name.endswith(".json") and not e.name.startswith(".") and e.is_file())
    except OSError:
        drop_ins = []
    return [base / "managed-settings.json"] + [base / "managed-settings.d" / name for name in drop_ins]


def settings_files():
    """(label, path) lowest to highest precedence."""
    cwd = Path.cwd()
    files = [("user settings", config_dir() / "settings.json"), ("project settings", cwd / ".claude" / "settings.json"),
             ("local settings", cwd / ".claude" / "settings.local.json")]
    return files + [("managed settings", p) for p in managed_settings_paths()]


_LAYERS = None


def settings_layers():
    """(label, path, settings dict) lowest to highest precedence; files that are missing or not a JSON object are skipped."""
    global _LAYERS
    if _LAYERS is None:
        _LAYERS = [(label, path, data) for label, path in settings_files() for data in [load_json(path, None)] if isinstance(data, dict)]
    return _LAYERS


def env_layers():
    """(label, path, env dict) lowest to highest precedence, for the files whose `env` is an object."""
    return [(label, path, data["env"]) for label, path, data in settings_layers() if isinstance(data.get("env"), dict)]


def settings_env(name):
    """(value, source) of `name` from the highest settings file whose env block sets it, else (None, None)."""
    for label, path, env in reversed(env_layers()):
        if env.get(name) not in (None, ""):
            value = env[name]
            return (json.dumps(value) if isinstance(value, bool) else str(value)), f"{label} {path}"
    return None, None


def session_var(name):
    """True for the variables a running session has already exported from its settings (and `check` scrubs)."""
    return (name.startswith("ANTHROPIC_DEFAULT_") and name.endswith("_MODEL")) or name in (FORK_VAR, SUBAGENT_VAR, FORCE_VAR)


def in_session():
    return bool(os.environ.get("CLAUDECODE"))


def shell_source(name):
    return STALE_SOURCE if in_session() and session_var(name) else "shell environment"


def env_setting(name):
    """(value, source) of an environment variable as Claude Code sees it: the highest settings file that sets it (Claude Code
    applies the settings env over the launch environment), else the shell."""
    value, src = settings_env(name)
    if value is not None:
        return value, src
    if os.environ.get(name):
        return os.environ[name], shell_source(name)
    return None, None


def shell_override_note(name):
    """A line when the launch environment also carries `name` but a settings file overrides it; None otherwise."""
    shell = os.environ.get(name)
    value, src = settings_env(name)
    if not shell or value is None or (in_session() and shell == value):  # a session exports the files' own value: not an override
        return None
    return f"{name}={shell} ({shell_source(name)}) is overridden by {name}={value} in the {src}: Claude Code applies the settings env over the launch environment."


def effective_env(name):
    return env_setting(name)[0]


def top_setting(key):
    """(value, source) of a top-level settings key from the highest file that sets it."""
    for label, path, data in reversed(settings_layers()):
        if data.get(key) not in (None, ""):
            return data[key], f"{label} {path}"
    return None, None


def higher_env_sources(name, above="user settings"):
    """Settings layers above `above` that also set `name` (they win over what `map`/`forks` write); never the shell, which loses to every file."""
    labels = [label for label, _ in settings_files()]
    return [f"{label} {path}" for label, path, env in env_layers() if labels.index(label) > labels.index(above) and env.get(name) not in (None, "")]


def provider():
    for flag, label in (("CLAUDE_CODE_USE_FOUNDRY", "Microsoft Foundry"), ("CLAUDE_CODE_USE_BEDROCK", "Amazon Bedrock"),
                        ("CLAUDE_CODE_USE_VERTEX", "Google Vertex AI")):
        if effective_env(flag):
            return label
    return "Anthropic API gateway" if effective_env("ANTHROPIC_BASE_URL") else "Anthropic (direct)"


def main_model_line():
    model, src = top_setting("model")
    env_model, env_src = env_setting("ANTHROPIC_MODEL")
    if model and env_model:
        return f'{model}   ("model" in {src}; ANTHROPIC_MODEL={env_model} is also set in {env_src})'
    if model:
        return f'{model}   ("model" in {src})'
    if env_model:
        return f"{env_model}   (ANTHROPIC_MODEL from {env_src})"
    return "(default)"


def fork_gate():
    """(state, detail): the fork feature is off when CLAUDE_CODE_FORK_SUBAGENT parses as false, the way Claude Code parses booleans."""
    value, src = env_setting(FORK_VAR)
    if value is None:
        return "on", ("Claude Code default: Agent launches are asynchronous and the fork agent type exists; `forks off` removes that type "
                      "and puts run_in_background back on the Agent tool")
    word = value.strip().lower()
    if word in ("0", "false", "no", "off"):
        return "off", f"{FORK_VAR}={value} ({src}): no fork agent type; the Agent tool has run_in_background again, launches still default to the background"
    if word in ("1", "true", "yes", "on"):
        return "on", f"{FORK_VAR}={value} ({src})"
    return "on", f"{FORK_VAR}={value} ({src}) is not a recognised boolean; Claude Code ignores it (default on)"


def subagent_model_lines():
    """Lines about CLAUDE_CODE_SUBAGENT_MODEL(_FORCE); the second element says whether _FORCE is set."""
    lines, forced = [], False
    value, src = env_setting(FORCE_VAR)
    if value:
        forced = True
        lines.append(f"WARNING    : {FORCE_VAR}={value} ({src}) forces every subagent onto that model: the Agent tool ignores the "
                     "model parameter and the agents' frontmatter, so tier routing has no effect until it is unset.")
    value, src = env_setting(SUBAGENT_VAR)
    if value:
        lines.append(f"note       : {SUBAGENT_VAR}={value} ({src}) is only the default for agents that name no model; "
                     "the router's agents all do and its hook sets one on every launch, so routing is unaffected.")
    return lines, forced


def cmd_show(_args):
    print(f"config dir : {config_dir()}")
    print(f"provider   : {provider()}")
    print(f"main model : {main_model_line()}")
    print("tiers      :")
    invalid = invalid_tiers()
    for tier, model in tier_models().items():
        if tier in invalid:
            print(f"  {tier:15} -> {model}  (default; stored value {invalid[tier]!r} is INVALID and ignored)")
        else:
            print(f"  {tier:15} -> {model}{'' if tier in stored_tiers() else '  (default)'}")
    print("alias maps :")
    mapped = {alias: env_setting(var) for alias, var in ALIAS_ENV.items() if effective_env(var)}
    for alias, (value, src) in mapped.items():
        print(f"  {alias:9} -> {value}   ({ALIAS_ENV[alias]}, {src})")
    if not mapped:
        print("  (none -- each alias resolves to Claude Code's default model id)")
    state, detail = fork_gate()
    print(f"fork gate  : {state}  ({detail})")
    lines, _ = subagent_model_lines()
    for line in lines:
        print(line)
    watched = list(ALIAS_ENV.values()) + [FORK_VAR, SUBAGENT_VAR, FORCE_VAR]
    for note in filter(None, map(shell_override_note, watched)):
        print(f"note       : {note}")
    if any("settings" in (env_setting(v)[1] or "") for v in watched):
        print("note       : a --settings file or JSON passed on the command line overrides the user, project and local files (not managed settings) and is not visible here.")
    return 0


# --- check -----------------------------------------------------------------------------------

def family(model_id):
    """The alias a model id belongs to; None when unknown or when the id names more than one."""
    found = [a for a in ALIASES if a in (model_id or "").lower()]
    return found[0] if len(found) == 1 else None


def result_record(stdout):
    """The `{"type": "result", ...}` object from `claude -p --output-format json`: the last JSON line, warnings before it ignored."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    for candidate in [line.strip() for line in reversed(lines)] + [stdout.strip()]:
        if not candidate or candidate[0] not in "{[":
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, list):
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
        if isinstance(data, dict) and data.get("type") == "result":
            return data
    return None


def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def check_one(model):
    argv = [shutil.which("claude") or "claude", "-p", "Reply with the single word OK.", "--model", model, "--output-format", "json", "--max-turns", "1",
            "--tools", "", "--system-prompt", "Answer with exactly what is asked.", "--no-session-persistence", "--strict-mcp-config",
            "--max-budget-usd", PROBE_BUDGET_USD]
    env = dict(os.environ)
    if env.get("CLAUDECODE"):  # the session exported its settings env; drop it so the child applies the settings files itself
        for key in [k for k in env if session_var(k)]:
            del env[key]
    started = time.time()
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=CHECK_TIMEOUT, env=env)
    except FileNotFoundError:
        return {"model": model, "status": "FAIL", "detail": "`claude` not found on PATH"}
    except OSError as exc:
        return {"model": model, "status": "FAIL", "detail": f"could not run claude: {exc}"[:200]}
    except subprocess.TimeoutExpired:
        return {"model": model, "status": "FAIL", "detail": f"no answer within {CHECK_TIMEOUT}s"}
    seconds = time.time() - started

    result = result_record(proc.stdout)
    if result is None:
        detail = last_line(proc.stderr) or last_line(proc.stdout) or f"exit code {proc.returncode}"
        return {"model": model, "status": "FAIL", "detail": detail[:200]}
    out = {"model": model, "seconds": round(seconds, 1), "cost_usd": result.get("total_cost_usd")}
    if result.get("is_error") or result.get("subtype") != "success":
        detail = str(result.get("result") or result.get("subtype") or "error")
        if result.get("api_error_status"):
            detail = f"HTTP {result['api_error_status']}: {detail}"
        return {**out, "status": "FAIL", "detail": detail[:200]}

    # Claude Code also makes small helper calls on its fast model; the model that answered is
    # the one that carried the conversation context, not the one with the most output tokens.
    def context_tokens(usage):
        return sum(usage.get(k) or 0 for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))

    answered = sorted((result.get("modelUsage") or {}).items(), key=lambda kv: -context_tokens(kv[1]))
    answered_by = answered[0][0] if answered else "?"
    out.update(status="OK", answered_by=answered_by)
    wanted = family(effective_env(ALIAS_ENV[model]) or model) if model in ALIASES else family(model)
    got = family(answered_by)
    if wanted and got and got != wanted:
        out["status"] = "FALLBACK"
        out["detail"] = f"asked for {wanted}, but {answered_by} answered"
    return out


def money(value):
    return f"${value:.4f}" if isinstance(value, (int, float)) else "-"


def tier_summary(results):
    """Per-tier status lines plus a ready-to-copy `set` line; the second element is True iff every tier's alias is OK."""
    status = {r["model"]: r["status"] for r in results}
    lines, fixes, all_ok = [], [], True
    for tier, alias in tier_models().items():
        state = status.get(alias, "not checked")
        all_ok = all_ok and state == "OK"
        line = f"  {tier:15} {alias:7} {state}"
        if state != "OK":
            better = next((a for a in ALIASES[ALIASES.index(alias):] if status.get(a) == "OK"), None)
            if better:
                line += f"  -> cheapest alias at or above {alias} that answered OK: {better}"
                fixes.append(f"{tier}={better}")
            else:
                unprobed = [a for a in ALIASES[ALIASES.index(alias):] if a not in status]
                cheaper = [a for a in ALIASES[:ALIASES.index(alias)] if status.get(a) == "OK"]
                line += "  -> no alias at or above it answered OK" + (f"; try `check --all` (not probed: {', '.join(unprobed)})" if unprobed else "; map it to a working deployment")
                line += f"; cheaper and OK: {', '.join(cheaper)}" if cheaper else ""
        lines.append(line)
    if fixes:
        lines.append(f"to apply: python3 {Path(__file__).resolve()} set {' '.join(fixes)}")
    return lines, all_ok


def cmd_check(args):
    everything = "--all" in args
    named = [a for a in args if a != "--all"]
    if any(a.startswith("-") for a in named):
        print("usage: models.py check [--all] [model ...]", file=sys.stderr)
        return 2
    aliases = ALIASES if everything else ("haiku", "sonnet")
    models = list(dict.fromkeys([a for a in ALIASES if a in list(tier_models().values()) + list(aliases)] + named))
    rounds = -(-len(models) // CHECK_WORKERS)
    print(f"provider: {provider()}   config dir: {config_dir()}")
    print(f"checking {', '.join(models)} -- one minimal request each, {CHECK_WORKERS} at a time, up to {CHECK_TIMEOUT}s per model "
          f"(worst case about {rounds * CHECK_TIMEOUT}s)\n")
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        results = list(pool.map(check_one, models))
    print(f"{'requested':28} {'status':9} {'answered by':34} {'time':>6} {'cost':>8}  detail")
    for r in results:
        print(f"{r['model']:28} {r['status']:9} {r.get('answered_by', '-'):34} {(str(r['seconds']) + 's') if 'seconds' in r else '-':>6} {money(r.get('cost_usd')):>8}  {r.get('detail', '')}")
    total = sum(r["cost_usd"] for r in results if isinstance(r.get("cost_usd"), (int, float)))
    print(f"{'total':28} {'':9} {'':34} {'':>6} {money(total):>8}")
    lines, all_ok = tier_summary(results)
    print("\ntiers:")
    print("\n".join(lines))
    print("\n" + json.dumps({"results": results, "tiers": tier_models(), "tiers_ok": all_ok}))
    return 0 if all_ok else 1


# --- writers ---------------------------------------------------------------------------------

def write_router_config(saved):
    path = router_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_set(args):
    chosen = {}
    for arg in args:
        tier, _, model = arg.partition("=")
        if tier not in TIERS or model not in ALIASES:
            print(f"invalid: {arg!r} -- use <tier>=<alias>, tier one of {', '.join(TIERS)}, alias one of {', '.join(ALIASES)}", file=sys.stderr)
            return 2
        chosen[tier] = model
    if not chosen:
        print("nothing to set", file=sys.stderr)
        return 2
    saved = stored_tiers()
    saved.update(chosen)
    print(f"saved {chosen} -> {write_router_config(saved)}\nTakes effect in sessions started from now on.")
    return 0


def cmd_reset(args):
    bad = [a for a in args if a not in TIERS]
    if bad:
        print(f"unknown tier(s) {', '.join(bad)}; one of {', '.join(TIERS)}", file=sys.stderr)
        return 2
    saved = stored_tiers()
    removed = {tier: saved.pop(tier) for tier in (args or list(TIERS)) if tier in saved}
    if not removed:
        print(f"nothing stored for {', '.join(args or TIERS)}; the defaults already apply ({TIERS})")
        return 0
    write_router_config(saved)
    print(f"removed {removed} from {router_config_path()}; defaults apply: " +
          ", ".join(f"{t}={TIERS[t]}" for t in removed) + "\nTakes effect in sessions started from now on.")
    return 0


def backup_path(path):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S.%f")
    candidate, n = path.with_name(f"{path.name}.bak-{stamp}"), 1
    while candidate.exists():
        candidate, n = path.with_name(f"{path.name}.bak-{stamp}-{n}"), n + 1
    return candidate


def atomic_write(path, text):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_settings_env(key, value):
    """Set `key` (remove it when value is None) in the env block of the user's settings.json: validate, then back up, then write atomically."""
    global _LAYERS
    path = config_dir() / "settings.json"
    settings = load_json(path, None) if path.exists() else {}
    if settings is None:
        print(f"{path} exists but could not be parsed; not touching it", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"{path} is not a JSON object; not touching it", file=sys.stderr)
        return 1
    env = settings.get("env")
    env = {} if env is None else env
    if not isinstance(env, dict):
        print(f'{path}: "env" is not an object; not touching it', file=sys.stderr)
        return 1
    others = higher_env_sources(key)
    if (value is None and key not in env) or (value is not None and env.get(key) == value):
        print(f"{key} {'is not set in' if value is None else 'already = ' + value + ' in'} {path}; nothing to change")
    else:
        if path.exists():
            backup = backup_path(path)
            shutil.copy2(path, backup)
            print(f"backup: {backup}")
        env = dict(env)
        if value is None:
            env.pop(key)
        else:
            env[key] = value
        if env:
            settings["env"] = env
        else:
            settings.pop("env", None)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        _LAYERS = None
        print(f"{key} {'removed from' if value is None else '= ' + value + ' in'} {path}\nTakes effect in sessions started from now on.")
    for src in others:
        print(f"NOTE: {key} is also set in the {src}, which overrides {path}.")
    note = shell_override_note(key)
    if note:
        print(f"NOTE: {note}")
    return 0


def write_alias_map(alias, value):
    if alias not in ALIASES:
        print(f"unknown alias {alias!r}; one of {', '.join(ALIASES)}", file=sys.stderr)
        return 2
    return write_settings_env(ALIAS_ENV[alias], value)


def cmd_map(args):
    if len(args) != 1 or "=" not in args[0]:
        print("usage: models.py map <alias>=<model-id>", file=sys.stderr)
        return 2
    alias, _, value = args[0].partition("=")
    if not value.strip():
        print(f"usage: models.py map <alias>=<model-id> -- the value is empty; `unmap {alias}` removes a mapping", file=sys.stderr)
        return 2
    return write_alias_map(alias, value.strip())


def cmd_unmap(args):
    if len(args) != 1:
        print("usage: models.py unmap <alias>", file=sys.stderr)
        return 2
    return write_alias_map(args[0], None)


def cmd_forks(args):
    if args not in (["off"], ["on"]):
        print("usage: models.py forks off|on", file=sys.stderr)
        return 2
    code = write_settings_env(FORK_VAR, "false" if args == ["off"] else None)
    if code == 0:
        print("Fork feature off: no fork agent type (nothing left to deny) and the Agent tool has its run_in_background parameter back. "
              "Launches still run in the background by default; the routing rules tell the main session to pass run_in_background: false "
              "for builders and the coordinator (operators keep background: true)."
              if args == ["off"] else "Fork feature back to Claude Code's default: every Agent launch is asynchronous and the fork agent type exists.")
    return code


# --- doctor ----------------------------------------------------------------------------------

def plugin_root():
    return Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent.parent.parent)


def count_files(folder):
    try:
        return sum(1 for e in os.scandir(folder) if e.is_file())
    except OSError:
        return 0


def claude_version():
    exe = shutil.which("claude")
    if not exe:
        return None, None
    try:
        proc = subprocess.run([exe, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        return exe, (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "(no version output)"
    except (OSError, subprocess.SubprocessError) as exc:
        return exe, f"could not run --version: {exc}"


def plugin_enabled():
    """(key, value, source) of the highest settings file's enabledPlugins entry for model-router@<marketplace>, else (None, None, None)."""
    found = (None, None, None)
    for label, path, data in settings_layers():
        plugins = data.get("enabledPlugins")
        for key, value in (plugins.items() if isinstance(plugins, dict) else ()):
            if key.startswith("model-router@"):
                found = (key, value, f"{label} {path}")
    return found


def cmd_doctor(_args):
    """Read-only self-check. It reads the settings files on disk, so it cannot see a --settings file/JSON or a policy layer passed on the command line."""
    problems = []
    root = plugin_root()
    print(f"config dir     : {config_dir()}")
    print(f"python         : {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 9):
        problems.append("python is older than 3.9; the hooks need 3.9+")
    print(f"plugin root    : {root}  ({'CLAUDE_PLUGIN_ROOT' if os.environ.get('CLAUDE_PLUGIN_ROOT') else 'from this script location'})")
    rules = root / "rules" / "routing.md"
    if rules.is_file():
        print(f"rules file     : {rules} ({len(rules.read_text(encoding='utf-8', errors='replace')):,} chars)")
    else:
        print(f"rules file     : MISSING -- {rules}")
        problems.append("rules/routing.md is missing; nothing is injected into sessions")
    sys.path.insert(0, str(root / "hooks"))
    try:
        import router_context as rc
        cap = getattr(rc, "MAX_CONTEXT_CHARS", None)
        cap_text = f"Claude Code caps a hook's context at 8,000 chars; the hooks keep under {cap:,}" if isinstance(cap, int) else "Claude Code caps a hook's context at 8,000 chars"
        try:
            rules_text = rc.build_rules()
            print(f"injected rules : {len(rules_text):,} chars ({cap_text}" + ("; AT the cap, the tail may be cut" if isinstance(cap, int) and len(rules_text) >= cap else "") + ")")
        except Exception as exc:
            print(f"injected rules : build_rules() failed: {exc}")
            problems.append(f"build_rules() failed: {exc}")
        try:
            print(f"corrections    : {len(rc.build_corrections()):,} chars injected")
        except Exception as exc:
            print(f"corrections    : build_corrections() failed: {exc}")
        feedback = rc.feedback_path()
        rows = len(rc.correction_rows(feedback.read_text(encoding="utf-8", errors="replace"))) if feedback.is_file() else None
        print(f"feedback file  : {feedback} -- {'not created yet' if rows is None else f'{rows} correction row(s)'}")
    except Exception as exc:
        print(f"hooks          : cannot import hooks/router_context.py from {root / 'hooks'}: {exc}")
        problems.append(f"hooks/router_context.py could not be imported ({exc}); the hooks cannot inject the rules")
    print(f"session markers: {count_files(router_dir() / 'sessions')} in {router_dir() / 'sessions'}")
    print(f"reports        : {count_files(router_dir() / 'reports')} in {router_dir() / 'reports'}")
    invalid = invalid_tiers()
    parts = []
    for tier, model in tier_models().items():
        parts.append(f"{tier}={model}" + (f" (stored {invalid[tier]!r} INVALID, default used)" if tier in invalid else ("" if tier in stored_tiers() else " (default)")))
        if tier in invalid:
            problems.append(f"{router_config_path()}: {tier}={invalid[tier]!r} is not one of {', '.join(ALIASES)}; run `set` or `reset`")
    print(f"tier models    : {', '.join(parts)}")
    mapped = [f"{alias} -> {env_setting(var)[0]} ({env_setting(var)[1]})" for alias, var in ALIAS_ENV.items() if effective_env(var)]
    print(f"alias maps     : {'; '.join(mapped) if mapped else 'none'}")
    state, detail = fork_gate()
    print(f"fork gate      : {state}  ({detail})")
    lines, forced = subagent_model_lines()
    print(f"subagent model : {SUBAGENT_VAR}={effective_env(SUBAGENT_VAR) or '(unset)'}, {FORCE_VAR}={effective_env(FORCE_VAR) or '(unset)'}")
    for line in lines:
        print(line)
    for note in filter(None, map(shell_override_note, list(ALIAS_ENV.values()) + [FORK_VAR, SUBAGENT_VAR, FORCE_VAR])):
        print(f"note           : {note}")
    key, enabled, src = plugin_enabled()
    if key:
        print(f"plugin enabled : {key} = {json.dumps(enabled)} ({src})")
        if not enabled:
            problems.append(f"{key} is disabled in {src}; its hooks do not run")
    else:
        from_cache = "/plugins/cache/" in str(root) or "\\plugins\\cache\\" in str(root)
        print(f"plugin enabled : no enabledPlugins entry for model-router@<marketplace> in the settings files"
              + (" -- the plugin root is a marketplace install, so it should have one" if from_cache else " (expected for a --plugin-dir checkout)"))
        if from_cache:
            problems.append("no enabledPlugins entry for model-router@<marketplace> although it is installed from a marketplace; run `/plugin` and enable it")
    if forced:
        problems.append(f"{FORCE_VAR} is set; every subagent runs on that model and tier routing has no effect")
    exe, version = claude_version()
    print(f"claude on PATH : {f'{exe} -- {version}' if exe else 'not found (the `check` command needs it; the hooks do not)'}")
    print("problems: none" if not problems else "problems:")
    for p in problems:
        print(f"  - {p}")
    return 1 if problems else 0


COMMANDS = {"show": cmd_show, "check": cmd_check, "set": cmd_set, "reset": cmd_reset, "map": cmd_map, "unmap": cmd_unmap,
            "forks": cmd_forks, "doctor": cmd_doctor}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
