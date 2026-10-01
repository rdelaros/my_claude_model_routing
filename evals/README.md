# Routing evals

Twenty natural messages, in English and Spanish, each with the routing decision the rules should produce: which tier handles it, whether the main session waits instead of acting, whether the confirmation gate fires. They run with `claude plugin eval` (Claude Code 2.1.286 or later), which starts a fresh session per case with the plugin loaded, so the rules are injected the way they are in real use, and grades the transcript with a haiku judge against each case's `graders/criteria.md`.

```
claude plugin eval . --runs 1 --max-cost-usd 2 --threshold 0.8
```

Each case costs a few cents (a short conversation on your main model plus the judge). Run the suite before and after any change to `rules/routing.md` or the agent descriptions and compare the scores; a case that fails consistently is a rule to fix, not a case to delete. Add a case whenever you record a routing correction, with the message that was misrouted as the prompt.
