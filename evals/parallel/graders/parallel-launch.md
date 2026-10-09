---
type: llm
focus: trace
---
The assistant launched the two pipeline checks in parallel (two model-router:operator launches in one message, or one operator task covering both environments) and said what is running. Whether the launched agent itself succeeded does not matter (the eval workspace may lack the tools it needs). Trace records whose parent_tool_use_id is not null belong to a launched agent, not to the main session. Only the routing decision and what the assistant told the user are graded.
