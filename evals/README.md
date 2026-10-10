# Routing evals

Twenty natural messages, in English and Spanish, each with the routing decision the rules should produce: which tier handles it, whether the main session waits instead of acting, whether the confirmation gate fires. `claude plugin eval` (Claude Code 2.1.286 or later) runs each in a fresh session with the plugin loaded, so the rules are injected the way they are in real use, and grades the trace.

## Run

```
claude plugin eval . --scaffold --allow-tools Bash --ablation none --runs 1 --max-cost-usd 3 --threshold 0.8
```

- `--scaffold` runs each case's `scaffold.sh` in the case's empty workspace first: a git checkout of a fake `acme/api` project on a feature branch with a local origin, a GitLab CI file, a Terraform folder with a saved plan, a test suite, one uncommitted change and a 3,000-line failed job log. Without it the model has nothing to rebase, diagnose or edit and can only ask where the repository is. Read `scaffold.sh` before passing the flag: it runs as you.
- `--allow-tools Bash` lets the main session and the agents run commands. Bash is gated inside evals; without the grant a case still shows the routing decision (the `Agent` call), but the operator fails at once. `Edit` and `Write` stay gated; the two build cases list them, so `--allow-tools Bash Edit Write` lets a small edit happen in the main session.
- If every run errors at once with "a credentials file in this environment ... could not be followed (ENOENT)", the Bash grant was refused: the runner reads the credential pointer files it must hide from the sandbox (AWS config and credentials, the GCP application-default credentials, the kubeconfig, Anthropic profile configs, and whatever `KUBECONFIG`, `AWS_CONFIG_FILE`, `AWS_SHARED_CREDENTIALS_FILE` or `GOOGLE_APPLICATION_CREDENTIALS` name) and one of them is a dangling symlink or a path to a missing file. On WSL that is usually `~/.kube/config` or `~/.aws` pointing at a Windows path. `find ~ -maxdepth 3 -xtype l` lists dangling links under your home; fix or remove the one it names, or unset the variable for the run. Running without `--allow-tools Bash` also works: the routing decision is still graded, only the agents' own commands fail.
- `--ablation none` runs the plugin arm only. The default also runs every case without the plugin and reports the delta, at twice the cost: use it to show what the plugin changes, not while iterating on a rule.
- `--runs 1`: the runner's default is three runs per case.
- A run of a case costs about a tenth of a dollar on Linux with Claude Code 2.1.295 and nearer twenty cents on Windows with 2.1.286, where the suite took twenty minutes: set `--max-cost-usd 5` there, and do not wrap the command in `timeout`. `--case 'ops-*'` filters, `--json ~/router-evals.json` keeps the full result, the HTML report lands in `evals/results/` (git-ignored).

## How a case is graded

Each case directory holds `prompt.md` (the user message, with `max_turns` and the tools it may use), `case.yaml` (name, description, the scaffold) and `graders/`:

- `tool_used` graders are free and deterministic. They match a regex against the JSON input of each tool call: an `Agent` launch with `"subagent_type":"model-router:operator"` at least once; no `Bash` call matching `\bgit\b` in the main session (`min: 0`, `max: 0`); no `Read` of the long log; no `fork`; a coordinator launched without `"model":`.
- `tool_used` counts every call in the trace, the launched agents' included (their records carry `parent_tool_use_id`), so a case that expects the operator to run git cannot also demand `max: 0` on `Bash`; the first real run failed six cases that way. What the main session itself did or did not run is left to the judge, whose criteria say how to tell the two apart.
- One `llm` grader per case reads the whole trace (`focus: trace`; the runner's default judges the last message only, which is why the suite once failed every delegation case) and checks what no regex can: the answer was in Spanish, a one-line status was given, nothing was asked twice. Three haiku votes decide. The judge fails long replies and English text from a launched agent unless told otherwise, so every criterion says the reply's length does not matter and that records with a `parent_tool_use_id` belong to the agent; where a decision can be checked by `tool_used` alone (the confirmation line, all three commands in one task, both environments in the pipeline check) there is no judge.

A case has at most four graders of equal weight and passes at `--threshold 0.8`, so any single failing grader fails the case: every grader is one whose failure means a wrong decision or a criterion to fix (at 0.7 a four-grader case passed with one failure, which is how an essay in reply to a half-finished sentence once counted as a pass). The judge is shown the first and last twelve records of a long trace, so criteria judge the opening decision and the final reply, and the launch itself is left to `tool_used`. Each grader's explanation is in the HTML report and in the JSON (`cases[].arms[].runs[].graders[]`: `name`, `passed`, `explanation`, and for the judge `judgeVotes` and `evidence`, the text it was shown).

## Reading a failure

- A `tool_used` grader failed: the routing decision was wrong, or right for a reason the rules do not state. Read the trace, fix `rules/routing.md`, and add the message to the feedback file as a correction if it came from real use.
- Only the `llm` grader failed: read its `evidence`. A judge that saw the right decision but a wording it did not expect is a criteria problem; sharpen the criteria.
- The run errored (`scaffold failed`, `Reached maximum number of turns`): the case, not the rules.

Add a case whenever you record a routing correction, with the message that was misrouted as the prompt; copy `scaffold.sh` and `case.yaml` from any case that has them when the message needs a repository. A case that fails consistently is a rule to fix, not a case to delete.
