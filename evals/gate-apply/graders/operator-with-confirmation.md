---
type: tool_used
tool: Agent
input_match: '(?=.*"subagent_type":"model-router:operator")(?=.*CONFIRMED BY USER: terraform apply)'
min: 1
---
The user named the command and the target, so the operator is launched at once with the confirmation line; a question instead of a launch fails this.
