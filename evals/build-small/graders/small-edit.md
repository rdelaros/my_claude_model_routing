---
type: llm
focus: trace
---
A one-line edit in one file (hooks/ops-nudge.py). PASS if the assistant either made the edit itself in the main session (a read of the file followed by an Edit, a Write, or a shell command such as sed or python that changes NUDGE_AT from 3 to 4) or launched exactly one model-router:builder agent to make it; a builder that reports RESULT: failed because the Edit tool is disabled still counts as a correct launch. FAIL if the assistant launched the coordinator, an operator or any other agent, or did neither (ended with a question, or described the change without making or delegating it).
