---
type: llm
focus: trace
---
The assistant read app/auth.py (itself, or through Explore or model-router:operator) and explained the refresh decision: valid() is false when the token is missing or has less than REFRESH_MARGIN seconds left, and get() then calls refresh(). It did not open many files one by one.
