---
type: llm
focus: trace
---
PASS when the trace shows one Agent launch of model-router:builder for the change, whatever the builder reported and whatever the final reply says. Also PASS when the assistant made the one-line edit itself in the main session (an Edit, a Write, or a sed/python command after reading hooks/ops-nudge.py). FAIL only if the assistant launched the coordinator, an operator or another agent, or neither edited nor launched (ended with a question, or just described the change).
