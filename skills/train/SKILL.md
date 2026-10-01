---
name: train
description: Training mode for the model-router: explain every routing decision, ask the user whether it is right before acting, and record corrections. Use when the user wants to learn how the router works, tune it, or asks to train it. Arguments: quiet, review, off.
---

# Training mode

The user wants to learn how the router decides and to tune it. From this message until `/model-router:train off`, follow the routing rules exactly as before, but make every decision visible and let the user steer. Argument given: `$ARGUMENTS` (empty means interactive).

## Interactive (no argument)

For every user message that is work (not conversation):

1. **Decide and show.** Before launching anything, write three short lines:
   - *Routing*: tier, agent and model (or "here"), and the one rule that decides it, in plain words.
   - *Plan*: what the agent will be asked to do, or what you will do here; whether anything is gated and needs a yes.
   - *Ask*: "Right, or should it go elsewhere?"
2. **Wait for the answer.** A yes (any wording) starts the work. A different choice is a correction: do it the user's way, and append one row `| date | situation | routed to | should be | why |` to the feedback file named in the routing rules, with the user's reason in the last column. Then say in one line which rule the row changes.
3. **When the result comes back**, say what it cost in rough terms (one launch on haiku, or N calls in the main context) and whether, in hindsight, the tier was right.

For conversation (an explanation, a question, a half-finished thought), answer as usual and add nothing.

Keep every explanation under 60 words. Do not re-explain a decision the user already approved for the same kind of work in this session; say "same as before" and go.

## `quiet`

Explain each decision in one line (*Routing: operator, haiku — known git commands*) and proceed without asking. Corrections still come from the user's own messages and are recorded as above.

## `review`

Read the feedback file and list its rows grouped by "should be" tier. For each group, propose one sentence that could be folded into `rules/routing.md` so the row is no longer needed, and ask which to keep. Do not edit the rules yourself; the user owns them.

## `off`

Say "training mode off" and return to routing silently.
