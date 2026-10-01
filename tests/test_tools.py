"""Tests for tools/before_after.py and tools/session_report.py. Standard library only.

Both scripts are run the way a person runs them — as a subprocess on a config directory holding
a synthetic transcript — and the printed lines are checked with regexes, so formatting can move.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
ASYNC_ACK = ("Async agent launched successfully. (This tool result is internal metadata \u2014 never quote or paste "
             "any part of it, including the agentId below, into a user-facing reply.)\n\nagentId: a1")   # verbatim from Claude Code 2.1.286


def rec(kind, content, ts, **extra):
    r = {"type": kind, "uuid": f"{kind}-{ts}", "timestamp": f"2026-09-30T10:{ts // 60:02}:{ts % 60:02}.000Z", "sessionId": "s1",
         "cwd": "/w", "message": {"role": kind, "content": content}}
    r.update(extra)
    return r


def assistant(mid, content, ts, usage=None, model="claude-sonnet-4-5"):
    u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
         "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    u.update(usage or {})
    r = rec("assistant", content, ts)
    r["message"].update({"id": mid, "model": model, "usage": u})
    return r


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, text, ts, **extra):
    return rec("user", [{"type": "tool_result", "tool_use_id": tid, "content": text}], ts, **extra)


def transcript():
    """One main session with every record shape the scripts have to classify."""
    recs = [
        {"type": "attachment", "uuid": "att-1", "timestamp": "2026-09-30T10:00:00.000Z", "sessionId": "s1",
         "attachment": {"type": "hook_additional_context", "hookName": "SessionStart", "content": "## Model routing\n- rules"}},
        # 1. string prompt -> three Bash calls: the first assistant record is duplicated (same message id, same tool_use id);
        #    the second message is streamed as two records (same message id and usage, one tool_use block each)
        rec("user", "fix the bug", 10),
        assistant("m1", [tool_use("tu1", "Bash", command="git status")], 20,
                  {"cache_creation_input_tokens": 400, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}),
        assistant("m1", [tool_use("tu1", "Bash", command="git status")], 21,
                  {"cache_creation_input_tokens": 400, "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}),
        tool_result("tu1", "clean", 25, toolUseResult={"stdout": "clean"}),
        assistant("m2", [tool_use("tu2", "Bash", command="ls")], 30,
                  {"cache_creation_input_tokens": 600, "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 300}}),
        assistant("m2", [tool_use("tu2b", "Bash", command="pwd")], 31,
                  {"cache_creation_input_tokens": 600, "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 300}}),
        tool_result("tu2", "a b", 35, toolUseResult={"stdout": "a b"}),
        tool_result("tu2b", "/w", 36, toolUseResult={"stdout": "/w"}),
        # 2. list prompt with text -> a background Agent (acknowledgement only) and a foreground Agent with a long report
        rec("user", [{"type": "text", "text": "now delegate"}], 60),
        assistant("m3", [tool_use("tu3", "Agent", prompt="scan", run_in_background=True)], 70),
        tool_result("tu3", ASYNC_ACK, 75, toolUseResult={"status": "async_launched", "isAsync": True, "agentId": "a1", "description": "scan"}),
        assistant("m4", [tool_use("tu4", "Agent", prompt="review", model="haiku")], 80),
        assistant("m4", [tool_use("tu4", "Agent", prompt="review", model="haiku")], 81),      # duplicated record again
        tool_result("tu4", "x" * 5000, 90, toolUseResult={"status": "completed", "content": [{"type": "text", "text": "x" * 5000}]}),
        rec("user", "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n<result>short result</result>\n</task-notification>", 95, isMeta=False),
        # 3. prompt that is only a pasted image
        rec("user", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}], 120),
        assistant("m5", [{"type": "text", "text": "A screenshot of a terminal."}], 130),
        # 4. slash command prompt
        rec("user", "<command-name>/review</command-name>\n<command-message>review</command-message>\n<command-args></command-args>", 150),
        assistant("m6", [{"type": "text", "text": "Reviewed."}], 160),
        # 5. pasted image with an empty text block -> two Bash calls and a Skill whose tool_use blocks carry no id (all must count)
        rec("user", [{"type": "text", "text": ""}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}], 170),
        assistant("m7", [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}, {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                         {"type": "tool_use", "name": "Skill", "input": {"skill": "lint"}}], 180),
        # compaction summary: not a prompt
        rec("user", "This session is being continued from a previous conversation...", 200, isCompactSummary=True),
    ]
    return "\n".join(json.dumps(r) for r in recs) + "\n"


class ToolCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cfg = Path(cls.tmp.name) / "cfg"
        proj = cls.cfg / "projects" / "x"
        proj.mkdir(parents=True)
        (proj / "s1.jsonl").write_text(transcript(), encoding="utf-8")
        cls.env = {k: v for k, v in os.environ.items() if k not in ("MODEL_ROUTER_REPORT_CHARS", "SKIP_SESSION")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_tool(self, name):
        p = subprocess.run([sys.executable, str(TOOLS / name), str(self.cfg)], capture_output=True, text=True, env=self.env, cwd=self.tmp.name)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def line(self, out, pattern):
        m = re.search(pattern, out, re.M)
        self.assertIsNotNone(m, f"no line matching {pattern!r} in:\n{out}")
        return m

    def test_before_after(self):
        out = self.run_tool("before_after.py")
        m = self.line(out, r"^\[after\] sessions (\d+), prompts (\d+), compactions (\d+), api calls (\d+)")
        self.assertEqual([int(x) for x in m.groups()], [1, 5, 1, 7])     # 5 prompts; duplicated m1 and streamed m2 counted once each
        self.line(out, r"^\[before\] sessions 0, prompts 0, compactions 0, api calls 0")
        m = self.line(out, r"cache writes: 5-minute TTL ([\d,]+) tok \| 1-hour TTL ([\d,]+) tok")
        self.assertEqual([int(x.replace(",", "")) for x in m.groups()], [700, 300])   # 400 + (600 - 300) | 300; m2's usage once
        m = self.line(out, r"agent reports back: (\d+), mean [\d,]+ chars, over ([\d,]+): (\d+)")
        self.assertEqual(m.group(1), "2", "the async acknowledgement must not count as a report")
        self.assertEqual((m.group(2), m.group(3)), ("4,000", "1"))
        m = self.line(out, r"Bash/MCP calls in the main session per operational prompt: ([\d.]+)")
        # (tu1 + tu2 + tu2b) + (two id-less blocks) over 2 operational prompts: a duplicated block counts once, a second block
        # under the same message id still counts (3.0 without dedup, 2.0 if deduplicated by message id or by a None id)
        self.assertEqual(m.group(1), "2.5")
        self.line(out, r"subagent launches: 2, with a model set: 1")
        self.line(out, r"^\s*ops in main\s+prompts\s+2 ")
        self.line(out, r"^\s*delegated\s+prompts\s+1 ")
        self.line(out, r"^\s*thinking\s+prompts\s+2 ")      # the pasted image and the slash command
        self.line(out, r"^\s*building\s+prompts\s+0 ")

    def test_before_after_report_budget_env(self):
        env = dict(self.env, MODEL_ROUTER_REPORT_CHARS="6000")
        p = subprocess.run([sys.executable, str(TOOLS / "before_after.py"), str(self.cfg)], capture_output=True, text=True, env=env, cwd=self.tmp.name)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.line(p.stdout, r"agent reports back: 2, mean [\d,]+ chars, over 6,000: 0")

    def test_session_report(self):
        out = self.run_tool("session_report.py")
        self.line(out, r": 1 sessions$")
        self.line(out, r"^prompts: 5, compactions: 1$")
        m = self.line(out, r"cache writes: 5-minute TTL ([\d,]+) tok \| 1-hour TTL ([\d,]+) tok")
        self.assertEqual([int(x.replace(",", "")) for x in m.groups()], [700, 300])
        self.line(out, r"^\s*operational \(commands/MCP\)\s+2\s+3 ")       # m1 once + m2 once, and m7
        self.line(out, r"^\s*thinking \(answers\)\s+3\s+4 ")
        self.assertNotIn("building (edits files)", out)
        self.line(out, r"^\s*/review\s+1\s+\d+k")
        self.line(out, r"^\s*skill:lint\s+1\s+\d+k")      # an id-less block after another id-less block still counts
        self.line(out, r"^\s*<100k\s+7 ")
        self.assertIn("('None', 'haiku'): 1", out)      # the duplicated m4 record counts once
        self.assertIn("('None', 'None'): 1", out)
        self.assertIn("'claude-sonnet-4-5': 7", out)


if __name__ == "__main__":
    unittest.main()
