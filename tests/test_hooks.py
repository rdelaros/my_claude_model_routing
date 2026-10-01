"""Tests for the model-router hooks. Standard library only: `python3 -m unittest discover -s tests`.

Every hook is run the way Claude Code runs it — as a subprocess with a JSON payload on stdin
and CLAUDE_CONFIG_DIR / CLAUDE_PLUGIN_ROOT in the environment — against a temporary config
directory, so a passing suite means the installed scripts work, not just their functions.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))
import router_context  # noqa: E402

OPS_CALL = {"tool_name": "Bash", "tool_input": {"command": "git status"}, "tool_use_id": "t1", "tool_response": "ok"}


def iso(seconds_ago=0):
    return datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "cfg"
        self.cfg.mkdir()
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.cfg), "CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for var in list(self.env):
            if var.startswith("MODEL_ROUTER_") or var.startswith("CLAUDE_CODE_SUBAGENT_MODEL"):
                del self.env[var]
        self.env["MODEL_ROUTER_DEBUG"] = "1"  # a crashing hook must fail the test, not pass as a silent no-op
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, script, payload, *args, env=None, plugin_root=None):
        env = {**self.env, **(env or {})}
        if plugin_root:
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
        proc = subprocess.run([sys.executable, str(HOOKS / script), *args], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "", proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def write_transcript(self, records, ensure_ascii=True):
        path = Path(self.tmp.name) / "transcript.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=ensure_ascii) + "\n" for r in records), encoding="utf-8")
        return str(path)


class RulesContext(HookCase):
    def test_rules_fit_the_cap_and_keep_the_feedback_line(self):
        out = self.run_hook("session-start.py", {"session_id": "s1"}, "rules")
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertTrue(text.startswith(router_context.RULES_MARKER))
        self.assertLessEqual(len(text), router_context.MAX_CONTEXT_CHARS)
        self.assertLessEqual(text.count("\n") + 1, router_context.MAX_CONTEXT_LINES)
        self.assertTrue(text.endswith(f"Feedback file: `{self.cfg / 'model-router' / 'routing-feedback.md'}`"))
        self.assertNotIn("{builder_model}", text)
        self.assertNotIn("{operator_model}", text)
        self.assertNotIn("{senior_operator_model}", text)
        self.assertGreater(router_context.MAX_CONTEXT_CHARS - len(text), 600, "no headroom for a long config path")

    def test_configured_tier_models_are_filled_in(self):
        (self.cfg / "model-router").mkdir()
        (self.cfg / "model-router" / "config.json").write_text(json.dumps({"builder": "opus", "operator": "nonsense"}))
        text = self.run_hook("session-start.py", {"session_id": "s1"}, "rules")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("`model-router:builder` (`opus`)", text)
        self.assertIn("`model-router:operator` (`haiku`)", text)  # invalid value falls back to the default

    def test_long_rules_are_cut_at_a_line_and_the_feedback_line_survives(self):
        fake = Path(self.tmp.name) / "plugin"
        (fake / "rules").mkdir(parents=True)
        (fake / "rules" / "routing.md").write_text("## Model routing\n" + "\n".join(f"- rule {i} " + "x" * 60 for i in range(300)))
        text = self.run_hook("session-start.py", {"session_id": "s1"}, "rules", plugin_root=fake)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), router_context.MAX_CONTEXT_CHARS)
        self.assertLessEqual(text.count("\n") + 1, router_context.MAX_CONTEXT_LINES)
        self.assertIn(router_context.TRUNCATED_NOTE, text)
        self.assertTrue(text.endswith("routing-feedback.md`"))
        self.assertRegex(text, r"- rule \d+ x+\n\(rules truncated", "cut must fall on a line boundary")

    def test_a_single_over_long_line_is_cut_too(self):
        fake = Path(self.tmp.name) / "plugin"
        (fake / "rules").mkdir(parents=True)
        (fake / "rules" / "routing.md").write_text("## Model routing " + "y" * 9000)
        text = self.run_hook("session-start.py", {"session_id": "s1"}, "rules", plugin_root=fake)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), router_context.MAX_CONTEXT_CHARS)
        self.assertIn(router_context.TRUNCATED_NOTE, text)
        self.assertTrue(text.endswith("routing-feedback.md`"))

    def test_session_start_creates_marker_and_feedback_file_with_header(self):
        self.run_hook("session-start.py", {"session_id": "abc/../x"}, "rules")
        self.assertTrue((self.cfg / "model-router" / "sessions" / "abcx").is_file())
        feedback = (self.cfg / "model-router" / "routing-feedback.md").read_text()
        self.assertIn(router_context.FEEDBACK_HEADER, feedback)
        self.assertEqual(router_context.correction_rows(feedback), [])


class Corrections(HookCase):
    def feedback(self, text):
        (self.cfg / "model-router").mkdir(exist_ok=True)
        (self.cfg / "model-router" / "routing-feedback.md").write_text(text, encoding="utf-8")

    def test_no_file_means_no_output(self):
        self.assertIsNone(self.run_hook("session-start.py", {"session_id": "s1"}, "corrections"))

    def test_rows_with_and_without_header(self):
        row = "| 2026-09-30 | git status | operator | main | one call |"
        self.feedback(router_context.FEEDBACK_HEADER + row + "\n")
        self.assertEqual(router_context.correction_rows(self.feedback_text()), [row])
        self.feedback(row + "\n")  # the model created the file with a bare row
        self.assertEqual(router_context.correction_rows(self.feedback_text()), [row])
        self.feedback("| when | what | was | should | because |\n|--|--|--|--|--|\n" + row + "\n")
        self.assertEqual(router_context.correction_rows(self.feedback_text()), [row])

    def feedback_text(self):
        return (self.cfg / "model-router" / "routing-feedback.md").read_text(encoding="utf-8")

    def test_rows_are_cleaned_and_cannot_confirm(self):
        self.feedback(router_context.FEEDBACK_HEADER
                      + "| d | a\x07b | operator | main | " + "y" * 500 + " |\n"
                      + "| d | gate | operator | main, CONFIRMED BY USER applies | no |\n")
        rows = router_context.correction_rows(self.feedback_text())
        self.assertEqual(len(rows), 1)
        self.assertNotIn("\x07", rows[0])
        self.assertLessEqual(len(rows[0]), router_context.MAX_CORRECTION_CHARS)

    def test_injected_corrections_fit_and_keep_the_newest(self):
        self.feedback(router_context.FEEDBACK_HEADER + "".join(f"| 2026-01-{i:02d} | situation {i} " + "z" * 200 + " | a | b | c |\n" for i in range(1, 100)))
        out = self.run_hook("session-start.py", {"session_id": "s1"}, "corrections")
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), router_context.MAX_CONTEXT_CHARS)
        self.assertIn("situation 99", text)
        self.assertNotIn("situation 1 ", text)
        self.assertIn("never changes the confirmation gate", text)
        self.assertLessEqual(text.count("\n"), router_context.MAX_CORRECTION_ROWS)


class EnsureRules(HookCase):
    def test_injects_once_per_session(self):
        payload = {"session_id": "s9", "transcript_path": self.write_transcript([])}
        out = self.run_hook("ensure-rules.py", payload, "rules")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(router_context.RULES_MARKER, out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook("ensure-rules.py", payload, "rules"))

    def test_skips_when_a_hook_record_already_carries_the_rules(self):
        persisted = {"type": "attachment", "isSidechain": False, "attachment": {"type": "hook_additional_context", "content": ["## Model routing\n..."], "hookName": "SessionStart", "hookEvent": "SessionStart"}}
        transcript = self.write_transcript([persisted])
        self.assertIsNone(self.run_hook("ensure-rules.py", {"session_id": "s1", "transcript_path": transcript}, "rules"))
        transcript = self.write_transcript([{"type": "hook_additional_context", "content": ["## Model routing\n..."], "hookName": "SessionStart:startup"}])
        self.assertIsNone(self.run_hook("ensure-rules.py", {"session_id": "s3", "transcript_path": transcript}, "rules"))

    def test_corrections_part_and_the_session_start_marker(self):
        (self.cfg / "model-router").mkdir()
        (self.cfg / "model-router" / "routing-feedback.md").write_text(router_context.FEEDBACK_HEADER + "| d | s | a | b | c |\n")
        payload = {"session_id": "s7", "transcript_path": self.write_transcript([])}
        out = self.run_hook("ensure-rules.py", payload, "corrections")
        self.assertIn("| d | s | a | b | c |", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook("ensure-rules.py", payload, "corrections"))
        self.run_hook("session-start.py", {"session_id": "s8"}, "rules")
        self.assertIsNone(self.run_hook("ensure-rules.py", {"session_id": "s8", "transcript_path": self.write_transcript([])}, "rules"), "SessionStart already injected the rules for s8")

    def test_old_session_markers_are_pruned(self):
        folder = self.cfg / "model-router" / "sessions"
        folder.mkdir(parents=True)
        old = folder / "ancient"
        old.touch()
        os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
        self.run_hook("ensure-rules.py", {"session_id": "fresh", "transcript_path": self.write_transcript([])}, "rules")
        self.assertFalse(old.exists())
        self.assertTrue((folder / "fresh").exists())

    def test_a_tool_result_quoting_the_source_does_not_count(self):
        quoted = 'needle = "## Model routing"; record = b"hook_additional_context"'
        transcript = self.write_transcript([{"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": quoted}]}}])
        out = self.run_hook("ensure-rules.py", {"session_id": "s2", "transcript_path": transcript}, "rules")
        self.assertIsNotNone(out)


class AgentModel(HookCase):
    def launch(self, tool_input, env=None):
        return self.run_hook("agent-model.py", {"tool_name": "Agent", "tool_input": tool_input, "session_id": "s"}, env=env)

    def test_fills_the_tier_model_with_an_accepted_alias(self):
        for agent, alias in (("model-router:operator", "haiku"), ("model-router:builder", "sonnet"), ("model-router:senior-operator", "sonnet"), ("Explore", "haiku")):
            out = self.launch({"subagent_type": agent, "prompt": "x"})
            model = out["hookSpecificOutput"]["updatedInput"]["model"]
            self.assertEqual(model, alias)
            self.assertIn(model, router_context.ALIASES)

    def test_explicit_model_main_tier_and_unknown_agents_are_left_alone(self):
        self.assertIsNone(self.launch({"subagent_type": "model-router:operator", "model": "opus"}))
        self.assertIsNone(self.launch({"subagent_type": "model-router:coordinator"}))
        self.assertIsNone(self.launch({"subagent_type": "general-purpose"}))

    def test_configured_and_extra_pairs(self):
        (self.cfg / "model-router").mkdir()
        (self.cfg / "model-router" / "config.json").write_text(json.dumps({"operator": "sonnet", "builder": "haiku[1m]"}))
        self.assertEqual(self.launch({"subagent_type": "model-router:operator"})["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.assertEqual(self.launch({"subagent_type": "model-router:builder"})["hookSpecificOutput"]["updatedInput"]["model"], "sonnet", "invalid alias falls back to the default")
        env = {"MODEL_ROUTER_AGENT_MODELS": "my-scout = operator, my-reviewer=senior_operator, bad=nope"}
        self.assertEqual(self.launch({"subagent_type": "my-scout"}, env)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.assertEqual(self.launch({"subagent_type": "my-reviewer"}, env)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.assertIsNone(self.launch({"subagent_type": "bad"}, env))

    def test_force_variable_disables_the_fill(self):
        self.assertIsNone(self.launch({"subagent_type": "model-router:operator"}, {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "opus"}))

    def test_fork_is_denied_unless_allowed(self):
        out = self.launch({"subagent_type": "fork", "prompt": "x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertLess(len(out["hookSpecificOutput"]["permissionDecisionReason"]), 2000)
        self.assertIsNone(self.launch({"subagent_type": "fork", "prompt": "x"}, {"MODEL_ROUTER_ALLOW_FORK": "1"}))


class ContextWatch(HookCase):
    def records(self, *steps):
        """steps: ('p', text) typed prompt, ('a', ctx) assistant call, ('c',) compaction summary."""
        out = []
        for s in steps:
            if s[0] == "p":
                out.append({"type": "user", "message": {"role": "user", "content": s[1]}, "timestamp": iso(30)})
            elif s[0] == "a":
                out.append({"type": "assistant", "timestamp": iso(10), "message": {"model": "m", "usage": {"input_tokens": 5, "cache_read_input_tokens": s[1] - 5}}})
            else:
                out.append({"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued from a previous conversation..."}})
        return out

    def watch(self, records, prompt="next", env=None, ensure_ascii=True):
        env = {"MODEL_ROUTER_CACHE_TTL": "100000", **(env or {})}  # the growth branch, never the expiry branch, unless a test says so
        return self.run_hook("context-watch.py", {"session_id": "s", "transcript_path": self.write_transcript(records, ensure_ascii), "prompt": prompt}, env=env)

    def test_warns_when_the_threshold_is_first_passed(self):
        out = self.watch(self.records(("p", "one"), ("a", 100000), ("p", "two"), ("a", 155000)))
        self.assertIn("155k tokens", out["systemMessage"])
        self.assertIn("every tool call is billed", out["systemMessage"])
        self.assertNotIn("expired", out["systemMessage"])

    def test_does_not_warn_twice_for_the_same_size(self):
        base = self.records(("p", "one"), ("a", 100000), ("p", "two"), ("a", 155000))
        self.assertIsNotNone(self.watch(base + self.records(("p", "next")), prompt="next"), "first prompt after the growth, already in the transcript")
        self.assertIsNone(self.watch(base + self.records(("p", "three"))), "the user already saw 155k at prompt three, which got no answer")
        self.assertIsNone(self.watch(base + self.records(("p", "three"), ("a", 155000))))

    def test_warns_again_after_a_step_of_growth(self):
        out = self.watch(self.records(("p", "one"), ("a", 155000), ("p", "two"), ("a", 210000)))
        self.assertIn("210k", out["systemMessage"])
        self.assertIn("every tool call is billed", out["systemMessage"])

    def test_silent_after_compaction(self):
        self.assertIsNone(self.watch(self.records(("p", "one"), ("a", 140000), ("p", "two"), ("a", 190000), ("c",))))

    def test_expired_cache_message(self):
        recs = self.records(("p", "one"), ("a", 200000))
        recs[-1]["timestamp"] = iso(3600)
        out = self.watch(recs, env={"MODEL_ROUTER_CACHE_TTL": "1"})
        self.assertIn("expired", out["systemMessage"])
        self.assertIn("200k", out["systemMessage"])
        recs = self.records(("p", "one"), ("a", 200000), ("p", "two"), ("a", 200000))  # the size was already seen: no growth warning
        recs[-1]["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 50}
        recs[-1]["timestamp"] = iso(3000)
        self.assertIsNone(self.watch(recs, env={"MODEL_ROUTER_CACHE_TTL": ""}), "a 50-minute-old call is within a detected 1-hour cache")
        self.assertIn("expired", self.watch(recs, env={"MODEL_ROUTER_CACHE_TTL": "600"})["systemMessage"], "an explicit TTL wins over the detected one")
        self.assertIn("160k", self.watch(self.records(("p", "one"), ("a", 100000), ("p", "x"), ("a", 160000)), env={"MODEL_ROUTER_COMPACT_TOKENS": "160000"})["systemMessage"])
        self.assertIsNone(self.watch(self.records(("p", "one"), ("a", 100000), ("p", "x"), ("a", 160000)), env={"MODEL_ROUTER_COMPACT_TOKENS": "170000"}))
        self.assertIsNone(self.watch(recs, env={"MODEL_ROUTER_CONTEXT_WATCH": "0"}))

    def test_line_separator_inside_a_record_does_not_break_parsing(self):
        recs = self.records(("p", "one"), ("a", 100000), ("p", "x"), ("a", 160000))
        recs[-1]["message"]["content"] = [{"type": "text", "text": "line one\u2028line two\u2029three\u0085four"}]
        raw = self.write_transcript(recs, ensure_ascii=False)
        self.assertIn("\u2028", Path(raw).read_text(encoding="utf-8"), "the transcript must hold the literal separator")
        self.assertIn("160k", self.watch(recs, ensure_ascii=False)["systemMessage"], "the 160k record holds the separators and must still be parsed")


class OpsNudge(HookCase):
    def batch(self, calls, session="s1", **extra):
        return self.run_hook("ops-nudge.py", {"session_id": session, "hook_event_name": "PostToolBatch", "tool_calls": calls, **extra}, "count")

    def test_nudges_once_when_the_threshold_is_crossed(self):
        self.assertIsNone(self.batch([OPS_CALL, {"tool_name": "Read", "tool_input": {}}]))
        self.assertIsNone(self.batch([OPS_CALL]))
        out = self.batch([{"tool_name": "mcp__jira__search", "tool_input": {}}, OPS_CALL])
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolBatch")
        self.assertIn("4 command/MCP calls", text)
        self.assertIn("mcp__jira__search", text)
        self.assertIn("git status", text)
        self.assertIsNone(self.batch([OPS_CALL]), "no second nudge at 5")
        self.assertIsNone(self.batch([OPS_CALL, OPS_CALL]))
        self.assertIn("Stop", self.batch([OPS_CALL])["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.batch([OPS_CALL]))
        self.assertTrue((self.cfg / "model-router" / "sessions" / "s1.ops").is_file())

    def test_reset_and_subagents(self):
        self.batch([OPS_CALL, OPS_CALL])
        self.run_hook("ops-nudge.py", {"session_id": "s1"}, "reset")
        self.assertIsNone(self.batch([OPS_CALL]))
        self.assertIsNone(self.batch([OPS_CALL] * 5, agent_id="agent-1"))
        out = self.batch([OPS_CALL] * 5, session="s2")
        self.assertIn("5 command/MCP calls", out["hookSpecificOutput"]["additionalContext"], "s2 starts its own counter at 0")
        self.assertIsNone(self.batch([OPS_CALL]), "s1 is still at 2 after the reset")
        self.assertTrue((self.cfg / "model-router" / "sessions" / "s2.ops").is_file())

    def test_disabled_by_env(self):
        self.assertIsNone(self.run_hook("ops-nudge.py", {"session_id": "s1", "tool_calls": [OPS_CALL] * 5}, "count", env={"MODEL_ROUTER_NUDGE_AT": "0"}))


class ReportBudget(HookCase):
    def stop(self, message, **extra):
        return self.run_hook("report-budget.py", {"session_id": "s", "agent_id": "a-1", "agent_type": "model-router:operator", "last_assistant_message": message, **extra})

    def test_short_reports_pass(self):
        self.assertIsNone(self.stop("short"))

    def test_long_reports_are_saved_and_sent_back(self):
        out = self.stop("x" * 5000)
        self.assertEqual(out["decision"], "block")
        path = re.search(r"Full report: (\S+)", out["reason"]).group(1)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(Path(path).read_text(), "x" * 5000)
        self.assertLess(len(out["reason"]), 2000)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(path).parent.stat().st_mode), 0o700)
        second = re.search(r"Full report: (\S+)", self.stop("y" * 5000)["reason"]).group(1)
        self.assertNotEqual(path, second, "a second long report from the same agent must not overwrite the first")

    def test_user_report_marker_passes_up_to_four_times_the_budget(self):
        self.assertIsNone(self.stop("REPORT FOR USER\n" + "| a | b |\n" * 1500))
        self.assertIsNone(self.stop("\nREPORT FOR USER \n" + "x" * 10000))
        self.assertEqual(self.stop("REPORT FOR USER\n" + "x" * 20000)["decision"], "block")
        self.assertEqual(self.stop("Here it is\nREPORT FOR USER\n" + "x" * 5000)["decision"], "block")

    def test_transcript_fallback_and_pruning(self):
        transcript = self.write_transcript([{"type": "assistant", "message": {"content": [{"type": "text", "text": "z" * 6000}]}}, {"type": "user", "message": {"content": "x"}}])
        out = self.run_hook("report-budget.py", {"agent_id": "a-2", "agent_type": "model-router:operator", "agent_transcript_path": transcript})
        self.assertEqual(out["decision"], "block")
        saved = Path(re.search(r"Full report: (\S+)", out["reason"]).group(1))
        self.assertEqual(saved.read_text(), "z" * 6000)
        stale = saved.parent / "stale-1.md"
        stale.write_text("old")
        os.utime(stale, (time.time() - 20 * 86400, time.time() - 20 * 86400))
        self.stop("y" * 5000)
        self.assertFalse(stale.exists(), "reports older than 14 days are deleted when a new one is saved")
        self.assertTrue(saved.exists())

    def test_skips(self):
        self.assertIsNone(self.stop("x" * 5000, stop_hook_active=True))
        self.assertIsNone(self.run_hook("report-budget.py", {"agent_type": "my-agent", "last_assistant_message": "x" * 5000}, env={"MODEL_ROUTER_REPORT_EXEMPT": "my-agent"}))
        self.assertIsNone(self.run_hook("report-budget.py", {"last_assistant_message": "x" * 5000}, env={"MODEL_ROUTER_REPORT_CHARS": "0"}))


class GuardWrites(HookCase):
    SCRATCH = "/tmp/claude-0/x/scratchpad"

    def bash(self, command, agent="model-router:operator", env=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "scratchpad_dir": self.SCRATCH, "session_id": "s"}
        if agent:
            payload["agent_type"] = agent
        out = self.run_hook("guard-writes.py", payload, env=env)
        return None if out is None else out["hookSpecificOutput"]["permissionDecision"]

    def test_denies_file_writes_from_operators(self):
        tmp = tempfile.gettempdir().replace("\\", "/")
        for cmd in ("echo hi > out.txt", 'echo x > "out.txt"', "cat <<EOF > notes.md\nx\nEOF", "ls | tee log.txt", "sed -i s/a/b/ f", "sed --in-place s/a/b/ f",
                    "perl -pi -e s/a/b/ f", "git push origin main --force", "git push -f", "git push -fu origin x", "git push +main", "git push --force-with-lease",
                    "git checkout -- file", "git checkout HEAD -- f", "git checkout .", "git checkout -f main", "git reset --hard HEAD~1", "git switch -C x",
                    "git clean -fd", "git apply fix.patch", "git -C /x apply p", "git -c k=v apply p", "cd repo && git restore .", "git rm f", "git mv a b", "git branch -D x",
                    "sudo sed -i s/a/b/ /etc/hosts", "sudo -E sed -i s/a/b/ f", "patch -p1 < x", "truncate -s 0 f", "echo x | sudo tee f", "cat a | xargs -I{} tee {}",
                    "nice sed -i s/a/b/ f", "time sed -i s/a/b/ f", "env X=1 sed -i s/a/b/ f", "X=1 sed -i s/a/b/ f", "command sed -i s/a/b/ f", "/usr/bin/sed -i s/a/b/ f",
                    "gsed -i s/a/b/ f", "find . -exec sed -i s/a/b/ {} +", "if true; then sed -i s/a/b/ f; fi", "for f in *; do sed -i s/a/b/ $f; done", "{ sed -i s/a/b/ f; }",
                    "echo `sed -i s/a/b/ f`", "cp a b", "mv a b", "rm -rf dir", "dd if=/dev/zero of=f", "install -m 644 a b", "rsync a b", "touch f", "mkdir d",
                    "python3 -c \"open('f','w').write('x')\"", "node -e 'require(\"fs\").writeFileSync(\"f\",\"x\")'", "bash -c 'echo x > f'", "sh -c \"echo x > f\"",
                    "eval 'echo x > f'", "curl -o f URL", "wget -O f URL", "tar xf a.tar", "unzip a.zip", "exec > file", "> file cat", "echo x > $TMPDIR/../x", "echo x | tee {}",
                    f"echo x > {tmp}/../other/file"):
            self.assertEqual(self.bash(cmd), "deny", cmd)
            self.assertEqual(self.bash(cmd, "model-router:senior-operator"), "deny", cmd)
            self.assertEqual(self.bash(cmd, "model-router:coordinator"), "deny", cmd)

    def test_allows_ordinary_operations(self):
        tmp = tempfile.gettempdir().replace("\\", "/")
        for cmd in ("git status", "git checkout -b feature", "git checkout main", "git push origin feature", "git commit -m 'fix > bug'", "cmd 2>&1 | tail -n 5",
                    "kubectl get pods -o yaml > /dev/null", "kubectl get pods | grep -v Running >/dev/null; echo $?", "(cmd >/dev/null)", "cmd 2>/dev/null|wc -l",
                    "cmd 2>/dev/null&&true", "ls > /dev/null 2>&1; echo done", "printf x > /dev/tty", "sed -n '1,5p' file", "sed -e 's/a/b/' file",
                    "terraform plan -out=plan.tfplan", "kubectl patch deploy x", "az webapp restart", "git rebase main", "git merge x", "git tag v1", "git -C /x log --oneline",
                    "git log --format='%h -> %s'", f"gh pr diff 42 > {self.SCRATCH}/diff.patch", f'gh pr diff 1 > "{self.SCRATCH}/pr 1.diff"', f"glab mr diff 7 >> {tmp}/mr.diff",
                    f"echo x > '{tmp}/y'", 'echo x > "$TMPDIR/y"', "echo x > $TMPDIR/y", "echo x > ${TMPDIR}/z", f'ls | tee "{self.SCRATCH}/t.log"',
                    f"echo ok | tee {self.SCRATCH}/a {tmp}/b", f"cp a.txt {tmp}/b.txt", f"rm {self.SCRATCH}/old.diff", f"mkdir -p {tmp}/work", f"curl -o {tmp}/x.json https://x",
                    f"tar xf a.tar -C {tmp}/u", "echo \"a > b\"", "awk '{print > \"x\"}' f", "git stash && git pull && git stash pop", "pytest -q 1>&2", "bash -c 'ls -la'",
                    "python3 -c \"print(open('f').read())\"", "python3 - <<'EOF'\nif a > b:\n    print(1)\nEOF", "psql -c x <<EOF\nselect * from t where a > 1;\nEOF",
                    "cat <<EOF\n<p>html</p>\nEOF", "echo a -> b", "cat f | tee >(wc -l)"):
            self.assertIsNone(self.bash(cmd), cmd)

    def test_other_agents_and_the_main_session_are_untouched(self):
        self.assertIsNone(self.bash("echo hi > out.txt", agent=None))
        self.assertIsNone(self.bash("echo hi > out.txt", agent="model-router:builder"))
        self.assertIsNone(self.bash("echo hi > out.txt", agent="general-purpose"))
        self.assertIsNone(self.bash("echo hi > out.txt", env={"MODEL_ROUTER_GUARD": "0"}))
        self.assertLess(len(self.run_hook("guard-writes.py", {"tool_name": "Bash", "agent_type": "model-router:operator", "tool_input": {"command": "x > " + "y" * 500}})["hookSpecificOutput"]["permissionDecisionReason"]), 400)


class AgentContext(HookCase):
    def test_plugin_agents_get_cwd_branch_and_scratchpad(self):
        out = self.run_hook("agent-context.py", {"agent_type": "model-router:operator", "agent_id": "a", "cwd": str(ROOT), "scratchpad_dir": "/tmp/s"})
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        self.assertIn("Working directory: " + str(ROOT), text)
        self.assertIn("git branch", text)
        self.assertIn("Scratchpad for saved output: /tmp/s", text)
        self.assertIn("CLAUDE.md is not loaded", text)
        self.assertLess(len(text), 600)
        builder = self.run_hook("agent-context.py", {"agent_type": "model-router:builder", "cwd": self.tmp.name})["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("CLAUDE.md", builder)
        self.assertNotIn("git branch", builder, "no branch outside a repository")
        self.assertIsNone(self.run_hook("agent-context.py", {"agent_type": "Explore", "cwd": str(ROOT)}))
        self.assertIsNone(self.run_hook("agent-context.py", {"cwd": str(ROOT)}))


class Definitions(unittest.TestCase):
    def frontmatter(self, path):
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), path)
        block = text.split("---\n", 2)[1]
        return dict(line.split(":", 1) for line in block.strip().splitlines()), text

    def test_hooks_json_points_at_existing_scripts_and_known_events(self):
        hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        known = {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch", "SubagentStart", "SubagentStop", "Stop", "SessionEnd", "PreCompact", "PostCompact", "Notification"}
        for event, groups in hooks.items():
            self.assertIn(event, known)
            for group in groups:
                for hook in group["hooks"]:
                    scripts = re.findall(r"/hooks/([\w-]+\.py)", hook["command"])
                    self.assertTrue(scripts, hook["command"])
                    for script in scripts:
                        self.assertTrue((HOOKS / script).is_file(), script)
                    self.assertIn("|| true", hook["command"])
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "startup|resume|clear|compact|fork")
        self.assertIn("PostToolBatch", hooks)
        self.assertIn("SubagentStart", hooks)

    @unittest.skipIf(os.name == "nt", "the hook commands are POSIX shell lines")
    def test_hook_commands_run_as_written(self):
        hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        with tempfile.TemporaryDirectory() as cfg:
            env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_CONFIG_DIR": cfg, "MODEL_ROUTER_DEBUG": "1"}
            payload = json.dumps({"session_id": "cmd", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_calls": [], "hook_event_name": "x"})
            for groups in hooks.values():
                for group in groups:
                    for hook in group["hooks"]:
                        proc = subprocess.run(hook["command"], shell=True, input=payload, capture_output=True, text=True, env=env, timeout=60)
                        self.assertEqual(proc.returncode, 0, hook["command"])
                        self.assertNotIn("Traceback", proc.stderr, hook["command"])

    def test_agent_frontmatter(self):
        ignored_for_plugin_agents = {"hooks", "permissionMode", "mcpServers"}
        for name in ("builder", "operator", "senior-operator", "coordinator"):
            keys, _ = self.frontmatter(ROOT / "agents" / f"{name}.md")
            self.assertEqual(keys["name"], " " + name)
            self.assertFalse(set(keys) & ignored_for_plugin_agents, name)
            self.assertIn(keys["model"].strip(), router_context.ALIASES + ("inherit",))
        for name in ("operator", "senior-operator"):
            keys, _ = self.frontmatter(ROOT / "agents" / f"{name}.md")
            self.assertEqual(keys["background"].strip(), "true")
            self.assertEqual(keys["omitClaudeMd"].strip(), "true")
            self.assertTrue(keys["maxTurns"].strip().isdigit())
            for tool in ("Edit", "Write", "NotebookEdit", "Agent"):
                self.assertIn(tool, keys["disallowedTools"])
        keys, _ = self.frontmatter(ROOT / "agents" / "coordinator.md")
        self.assertIn("Agent(model-router:builder)", keys["tools"])
        self.assertNotIn("Edit", keys["tools"])

    def test_operators_share_identical_gate_and_tool_rules(self):
        def section(text, heading):
            return re.search(r"^# " + re.escape(heading) + r"\n(.*?)(?=^# |\Z)", text, re.S | re.M).group(1)
        _, op = self.frontmatter(ROOT / "agents" / "operator.md")
        _, so = self.frontmatter(ROOT / "agents" / "senior-operator.md")
        for heading in ("Confirmation gate", "Tool rules (when used)"):
            self.assertEqual(section(op, heading), section(so, heading), heading)
        for text in (op, so):
            self.assertIn("REPORT FOR USER", text)
            self.assertIn("timeout: 600000", text)
            self.assertNotIn("10-minute Bash limit", text)
            self.assertIn("last line of your task is `CONFIRMED BY USER: <command> on <target>`", text)
            self.assertIn("Each tool-calling message is one turn", text)
            self.assertIn("RESULT: ok", text)

    def test_rules_mention_what_the_hooks_expect(self):
        rules = (ROOT / "rules" / "routing.md").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith(router_context.RULES_MARKER))
        for needle in ("{builder_model}", "{operator_model}", "{senior_operator_model}", "CONFIRMED BY USER: <command> on <target>", "REPORT FOR USER", "Full report:",
                       "| date | situation | routed to | should be | why |", "run_in_background", "operators do not load CLAUDE.md", "RESULT: ok", "Examples:", "Goal / Where"):
            self.assertIn(needle, rules)
        self.assertNotIn("Never write 3+ files here", rules)

    def test_no_false_foreground_claim_survives(self):
        for rel in ("README.md", "CHANGELOG.md", "skills/setup/SKILL.md", "skills/setup/scripts/models.py", "rules/routing.md"):
            text = (ROOT / rel).read_text(encoding="utf-8").lower()
            for phrase in ("restores foreground launches", "run in the foreground again", "returning their result directly"):
                self.assertNotIn(phrase, text, rel)

    def test_playbook_appendix_matches_the_report_script(self):
        doc = (ROOT / "docs" / "optimize-a-machine.md").read_text(encoding="utf-8")
        script = (ROOT / "tools" / "session_report.py").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", doc, re.S)
        self.assertTrue(any(b.strip() == script.strip() for b in blocks), "docs/optimize-a-machine.md appendix has drifted from tools/session_report.py")

    def test_eval_cases_are_well_formed(self):
        cases = [p for p in (ROOT / "evals").iterdir() if p.is_dir()]
        self.assertGreaterEqual(len(cases), 20)
        for case in cases:
            prompt = (case / "prompt.md").read_text(encoding="utf-8")
            self.assertTrue(prompt.startswith("---\nmax_turns:"), case.name)
            self.assertTrue(prompt.strip().split("---")[-1].strip(), "empty prompt in " + case.name)
            graders = list((case / "graders").glob("*.md"))
            self.assertTrue(graders, case.name)
            for g in graders:
                self.assertIn("type: llm", g.read_text(encoding="utf-8"))

    def test_versions_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], market["plugins"][0]["version"])
        self.assertIn(plugin["version"], (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        for word in ("senior-operator", "coordinator"):
            self.assertIn(word, plugin["description"])


if __name__ == "__main__":
    unittest.main()
