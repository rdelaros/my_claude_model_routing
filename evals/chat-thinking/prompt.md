---
max_turns: 4
allowed_tools: [Agent, Read, Glob, Grep, Bash, Skill]
---

I'm thinking we should split the auth module into token validation and session storage, because right now every change touches both and
