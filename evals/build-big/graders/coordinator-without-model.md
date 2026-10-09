---
type: tool_used
tool: Agent
input_match: '^(?=.*"subagent_type":"model-router:coordinator")(?!.*"model":")'
min: 1
---
A coordinator launch that carries no model key: the hook leaves the coordinator on the main model. The ^ anchor keeps both lookaheads at the start of the JSON.
