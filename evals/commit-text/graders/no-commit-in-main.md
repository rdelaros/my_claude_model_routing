---
type: tool_used
tool: Bash
input_match: '"command":"(?:[^"\\]|\\.)*\bgit\s+commit\b'
min: 0
max: 0
---
Matches the command value only, not a description that mentions the commit; an agent's commit attempt counts too.
