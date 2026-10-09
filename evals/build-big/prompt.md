---
max_turns: 5
timeout_seconds: 900
allowed_tools: [Agent, Read, Glob, Grep, Bash, Edit, Write]
---

the plan is agreed, implement it: 1) add app/reviewer.py with a Reviewer class that scores a diff, 2) add hooks/route-reviewer.py that routes review requests to it, 3) add tests/test_reviewer.py, 4) add a Reviewer section to the README. Tests run with pytest -q.
