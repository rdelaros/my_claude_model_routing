---
max_turns: 5
allowed_tools: [Agent, Read, Glob, Grep, Bash]
---

I'm thinking we should split the auth module into token validation and session storage, because right now every change touches both and
