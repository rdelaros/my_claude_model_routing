"""Tests for skills/setup/scripts/models.py, run as a subprocess against a temporary config directory.

`check` is exercised with a fake `claude` on PATH that prints a warning line before its JSON
result, so the real CLI is never called and nothing is spent.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "setup" / "scripts" / "models.py"


def load_models():
    spec = importlib.util.spec_from_file_location("setup_models", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (fake)"); sys.exit(0)
model = args[args.index("--model") + 1]
print("Warning: a stray line before the result")
leak = os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
if leak:  # a session-exported mapping that reached the probe: report it so the test can see whether it was scrubbed
    print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "leaked ANTHROPIC_DEFAULT_HAIKU_MODEL=" + leak})); sys.exit(1)
if model == "opus":
    print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "api_error_status": 404, "result": "model not found"})); sys.exit(1)
if model == "fable":
    print("garbage"); print("E: boom", file=sys.stderr); sys.exit(1)
answered = "claude-sonnet-4-5" if model in ("sonnet", "my-haiku-deployment") else "claude-" + model + "-4-5"
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0012,
                  "modelUsage": {answered: {"inputTokens": 900, "outputTokens": 3}, "claude-haiku-4-5": {"inputTokens": 40, "outputTokens": 1}}}))
'''


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "cfg"
        self.cfg.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith(("MODEL_ROUTER_", "CLAUDE_CODE_SUBAGENT_MODEL", "ANTHROPIC_DEFAULT_", "CLAUDE_CODE_FORK"))}
        self.env.update({"CLAUDE_CONFIG_DIR": str(self.cfg), "CLAUDE_PLUGIN_ROOT": str(ROOT), "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")})

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, env=None, cwd=None):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env={**self.env, **(env or {})}, cwd=cwd or self.tmp.name, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def settings(self):
        return json.loads((self.cfg / "settings.json").read_text(encoding="utf-8"))

    def backups(self):
        return sorted(p.name for p in self.cfg.glob("settings.json.bak-*"))

    def test_show_and_doctor_on_an_empty_config(self):
        code, out, _ = self.run_script("show")
        self.assertEqual(code, 0)
        self.assertIn("builder         -> sonnet  (default)", out)
        self.assertIn("fork gate  : on", out)
        code, out, _ = self.run_script("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("problems: none", out)
        self.assertRegex(out, r"injected rules : [\d,]+ chars")
        self.assertIn("9.9.9 (fake)", out)

    def test_doctor_reports_problems(self):
        code, out, _ = self.run_script("doctor", env={"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "opus"})
        self.assertEqual(code, 1)
        self.assertIn("FORCE", out)
        (self.cfg / "model-router").mkdir()
        (self.cfg / "model-router" / "config.json").write_text(json.dumps({"operator": "haiku[1m]"}))
        code, out, _ = self.run_script("doctor")
        self.assertEqual(code, 1)
        self.assertIn("INVALID", out)
        code, out, _ = self.run_script("doctor", env={"CLAUDE_PLUGIN_ROOT": self.tmp.name})
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)

    def test_set_and_reset(self):
        code, out, _ = self.run_script("set", "builder=opus", "operator=sonnet")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads((self.cfg / "model-router" / "config.json").read_text()), {"builder": "opus", "operator": "sonnet"})
        self.assertEqual(self.run_script("set", "builder=haiku[1m]")[0], 2)
        self.assertEqual(self.run_script("reset", "builder")[0], 0)
        self.assertEqual(json.loads((self.cfg / "model-router" / "config.json").read_text()), {"operator": "sonnet"})
        self.assertEqual(self.run_script("reset")[0], 0)
        self.assertEqual(json.loads((self.cfg / "model-router" / "config.json").read_text()), {})
        self.assertEqual(self.run_script("reset", "nope")[0], 2)

    def test_map_unmap_and_forks_on_edge_case_settings(self):
        (self.cfg / "settings.json").write_text("{}")
        code, out, _ = self.run_script("map", "haiku=my-haiku-deployment")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.settings()["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "my-haiku-deployment")
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.run_script("map", "haiku=my-haiku-deployment")[0], 0)
        self.assertEqual(len(self.backups()), 1, "a no-op map must not back up or rewrite")
        self.assertEqual(self.run_script("unmap", "sonnet")[0], 0)
        self.assertEqual(len(self.backups()), 1, "unmapping an unmapped alias must not back up")
        self.assertEqual(self.run_script("forks", "off")[0], 0)
        self.assertEqual(self.settings()["env"]["CLAUDE_CODE_FORK_SUBAGENT"], "false")
        self.assertIn("fork gate  : off", self.run_script("show")[1])
        self.assertEqual(self.run_script("forks", "on")[0], 0)
        self.assertNotIn("CLAUDE_CODE_FORK_SUBAGENT", self.settings().get("env", {}))
        self.assertEqual(self.run_script("unmap", "haiku")[0], 0)
        self.assertNotIn("env", self.settings())
        self.assertEqual(self.run_script("forks", "sideways")[0], 2)
        (self.cfg / "settings.json").write_text('{"env": null, "model": "opus"}')
        self.assertEqual(self.run_script("map", "sonnet=x")[0], 0)
        self.assertEqual(self.settings(), {"env": {"ANTHROPIC_DEFAULT_SONNET_MODEL": "x"}, "model": "opus"})
        before = len(self.backups())
        for bad in ('{"env": [1]}', "[1]", "not json", ""):
            (self.cfg / "settings.json").write_text(bad)
            self.assertEqual(self.run_script("map", "sonnet=x")[0], 1, bad)
            self.assertEqual((self.cfg / "settings.json").read_text(), bad, "a refused write must not touch the file")
        self.assertEqual(len(self.backups()), before, "a refused write must not leave a backup")
        self.assertEqual(list(self.cfg.glob(".settings.json.*.tmp")), [])

    def test_show_reads_project_and_shell_layers(self):
        project = Path(self.tmp.name) / "proj" / ".claude"
        project.mkdir(parents=True)
        (project / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "local-haiku"}}))
        code, out, _ = self.run_script("show", cwd=project.parent, env={"ANTHROPIC_DEFAULT_SONNET_MODEL": "shell-sonnet"})
        self.assertEqual(code, 0)
        self.assertIn("haiku     -> local-haiku", out)
        self.assertIn("local settings", out)
        self.assertIn("sonnet    -> shell-sonnet", out)
        self.assertIn("shell environment", out)
        code, out, _ = self.run_script("map", "haiku=user-haiku", cwd=project.parent)
        self.assertEqual(code, 0)
        self.assertIn("NOTE", out)

    def test_settings_files_beat_the_shell(self):
        (self.cfg / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "user-haiku"}}))
        code, out, _ = self.run_script("show", env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "shell-haiku"})
        self.assertEqual(code, 0)
        self.assertIn("haiku     -> user-haiku", out)
        self.assertNotIn("-> shell-haiku", out)
        self.assertIn("ANTHROPIC_DEFAULT_HAIKU_MODEL=shell-haiku (shell environment) is overridden by", out)
        code, out, _ = self.run_script("map", "haiku=other-haiku", env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "shell-haiku"})
        self.assertEqual(code, 0)
        self.assertIn("NOTE: ANTHROPIC_DEFAULT_HAIKU_MODEL=shell-haiku (shell environment) is overridden by", out)
        self.assertNotIn("also set in the shell", out)

    def test_running_session_exports_are_stale_and_scrubbed_for_check(self):
        session = {"CLAUDECODE": "1", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "old-haiku"}
        code, out, _ = self.run_script("show", env=session)
        self.assertEqual(code, 0)
        self.assertIn("haiku     -> old-haiku   (ANTHROPIC_DEFAULT_HAIKU_MODEL, exported by the running session (stale; restart to clear))", out)
        self.assertIn("exported by the running session (stale; restart to clear)", self.run_script("doctor", env=session)[1])
        (self.cfg / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "old-haiku"}}))
        self.assertNotIn("is overridden by", self.run_script("show", env=session)[1], "a session carrying the file's own value is not an override")
        code, out, _ = self.run_script("check", env={"ANTHROPIC_DEFAULT_HAIKU_MODEL": "old-haiku"})
        self.assertEqual(code, 1)
        self.assertIn("leaked ANTHROPIC_DEFAULT_HAIKU_MODEL=old-haiku", out, "outside a session the launch environment is passed through")
        code, out, _ = self.run_script("check", env=session)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"haiku\s+OK\s+claude-haiku-4-5")
        self.assertNotIn("leaked", out)

    def test_managed_settings_paths(self):
        m = load_models()
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(m.managed_settings_base(), Path("/Library/Application Support/ClaudeCode"))
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files (x86)"}):
            self.assertEqual(m.managed_settings_base(), Path(r"C:\Program Files\ClaudeCode"), "Claude Code hardcodes the path; %ProgramFiles% is not consulted")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(m.managed_settings_base(), Path("/etc/claude-code"))
        base = Path(self.tmp.name) / "managed"
        (base / "managed-settings.d").mkdir(parents=True)
        for name in ("20-b.json", "10-a.json", ".hidden.json", "notes.txt"):
            (base / "managed-settings.d" / name).write_text("{}")
        (base / "managed-settings.d" / "sub.json").mkdir()
        with mock.patch.object(m, "managed_settings_base", return_value=base):
            self.assertEqual(m.managed_settings_paths(), [base / "managed-settings.json", base / "managed-settings.d" / "10-a.json", base / "managed-settings.d" / "20-b.json"])
        with mock.patch.object(m, "managed_settings_base", return_value=base / "missing"):
            self.assertEqual(m.managed_settings_paths(), [base / "missing" / "managed-settings.json"])

    def test_doctor_reports_whether_the_plugin_is_enabled(self):
        (self.cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {"model-router@shop": False, "other@shop": True}}))
        code, out, _ = self.run_script("doctor")
        self.assertEqual(code, 1)
        self.assertIn("plugin enabled : model-router@shop = false (user settings", out)
        self.assertIn("model-router@shop is disabled", out)
        project = Path(self.tmp.name) / "proj" / ".claude"
        project.mkdir(parents=True)
        (project / "settings.json").write_text(json.dumps({"enabledPlugins": {"model-router@shop": True}}))
        code, out, _ = self.run_script("doctor", cwd=project.parent)
        self.assertEqual(code, 0, out)
        self.assertIn("plugin enabled : model-router@shop = true (project settings", out)
        (self.cfg / "settings.json").write_text("{}")
        code, out, _ = self.run_script("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("no enabledPlugins entry for model-router@<marketplace>", out)
        cached = Path(self.tmp.name) / "plugins" / "cache" / "shop" / "model-router"
        cached.mkdir(parents=True)
        for folder in ("rules", "hooks"):
            os.symlink(ROOT / folder, cached / folder, target_is_directory=True)
        code, out, _ = self.run_script("doctor", env={"CLAUDE_PLUGIN_ROOT": str(cached)})
        self.assertEqual(code, 1, out)
        self.assertIn("installed from a marketplace", out)

    def test_fork_gate_parses_booleans_like_claude_code(self):
        for value, state in (("off", "off"), ("OFF", "off"), ("0", "off"), ("no", "off"), ("on", "on"), ("TRUE", "on"), ("1", "on")):
            (self.cfg / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_FORK_SUBAGENT": value}}))
            out = self.run_script("show")[1]
            self.assertIn('fork gate  : {0}  (CLAUDE_CODE_FORK_SUBAGENT={1}'.format(state, value), out)
            self.assertNotIn("not a recognised boolean", out)
        (self.cfg / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_FORK_SUBAGENT": "maybe"}}))
        out = self.run_script("show")[1]
        self.assertIn("fork gate  : on  (CLAUDE_CODE_FORK_SUBAGENT=maybe", out)
        self.assertIn("not a recognised boolean; Claude Code ignores it (default on)", out)
        self.assertIn("Launches still run in the background by default", self.run_script("forks", "off")[1])
        self.assertNotIn("foreground", self.run_script("show")[1])

    def test_map_with_an_empty_value_is_a_usage_error(self):
        (self.cfg / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "x"}}))
        code, _, err = self.run_script("map", "haiku=")
        self.assertEqual(code, 2)
        self.assertIn("unmap haiku", err)
        self.assertEqual(self.settings()["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "x", "`map haiku=` must not unmap")
        self.assertEqual(self.run_script("map", "haiku=  ")[0], 2)

    def test_check_reports_an_unrunnable_claude(self):
        m = load_models()
        with mock.patch.object(m.subprocess, "run", side_effect=OSError(8, "Exec format error")):
            self.assertEqual(m.check_one("haiku"), {"model": "haiku", "status": "FAIL", "detail": "could not run claude: [Errno 8] Exec format error"})
        with mock.patch.object(m.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")):
            self.assertEqual(m.check_one("haiku")["detail"], "`claude` not found on PATH")

    def test_check_parses_results_and_recommends(self):
        code, out, _ = self.run_script("check")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"sonnet\s+OK\s+claude-sonnet-4-5")
        self.assertRegex(out, r"haiku\s+OK\s+claude-haiku-4-5")
        self.assertIn("$0.0012", out)
        self.assertIn("tiers_ok\": true", out)
        code, out, _ = self.run_script("check", "--all", "my-haiku-deployment")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"opus\s+FAIL\s+.*HTTP 404")
        self.assertRegex(out, r"fable\s+FAIL\s+.*E: boom")
        self.assertRegex(out, r"my-haiku-deployment\s+FALLBACK\s+claude-sonnet-4-5")
        (self.cfg / "model-router").mkdir()
        (self.cfg / "model-router" / "config.json").write_text(json.dumps({"builder": "opus"}))
        code, out, _ = self.run_script("check", "--all")
        self.assertEqual(code, 1)
        self.assertIn("builder         opus    FAIL", out)
        self.assertIn("cheaper and OK: haiku, sonnet", out)
        self.assertEqual(self.run_script("check", "--bogus")[0], 2)


if __name__ == "__main__":
    unittest.main()
