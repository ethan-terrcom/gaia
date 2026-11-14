# The lead: planning and review

> Package **11** · `gaia/orchestrator/{lead,repo_context}.py`, `gaia/domain/plans.py`,
> `gaia/orchestrator/prompts/{lead_plan,lead_review}.md`, `AnthropicClient.parse` · current as of 2026-11-14

## What it's for
The lead engineer (Claude) does two judgement jobs in every run:
- **plan:** turn a ticket into 1–10 small, independently testable steps for a junior;
- **review:** read a junior's diff and test output, and decide `accept`, `revise` or `escalate`.

Each job is **one structured-output call**. The model returns JSON matching a Pydantic model
(`Plan`, `Review`), so the orchestrator (doc 12) never parses prose.

## Files
| File | Role |
|---|---|
| `domain/plans.py` | `PlanStep`, `Plan`, `Review`, `TicketInput`: the shapes the model must return |
| `orchestrator/prompts/*.md` | the system prompts, **written by the lead and byte-stable** (no dates or ids), so prompt caching hits |
| `orchestrator/repo_context.py` | `build_repo_context`: README, a depth-2 tree, and files the ticket names, all through `confine()` |
| `orchestrator/lead.py` | `LeadEngineer.plan_ticket`, `review_step`, `final_review`, `truncate_diff` |
| `llm/anthropic_client.py` | `parse(system, messages, output_model)`: streamed structured outputs |

## How it works

### Planning
```python
engineer = LeadEngineer(client, CommandPolicy.from_default())
context = await build_repo_context(Path(repo.local_path), ticket)   # read-only
plan = await engineer.plan_ticket(ticket, context, extra_allow=[repo.test_command])
```
1. The ticket and repo context go into the **user** message, each inside `<untrusted_content>`.
   The system prompt says never to follow instructions found there.
2. `client.parse(..., output_model=Plan, enable_fallback=True)` runs. It streams, sets
   `effort: high`, and caches the system prompt. If Claude's classifiers refuse, the server-side
   fallback retries on another model.
3. Every `step.test_command` is checked with the command policy (09a). A `deny` triggers **one**
   retry: the rejected plan is replayed as the assistant turn, followed by the policy's reasons. A
   second `deny` raises `PlanRejected`. An `ask` is fine, because a human approves it at run time.
4. A refusal, even after fallback, raises `LeadEscalation(category)` and is never retried. The
   orchestrator turns it into an `escalation` approval.

### Review
`review_step(plan, step_index, diff, test_output)` and `final_review(plan, full_diff)` send the
plan, the step's acceptance criteria, and the (truncated) diff and test output. `truncate_diff`
caps each file at 20 KB and the total at 150 KB, and **tells the model what it cut**, so it never
assumes hidden code is fine.

### Why "always stream"?
An Opus call with `effort: high` can think for a while before emitting anything. Over a
non-streamed request, that is a silent connection: during the doc 08 spike one such call died
mid-flight. `parse` therefore uses `messages.stream(..., output_format=Plan)` plus
`get_final_message()`. The SDK still returns `parsed_output`. This also lifts the 16K
`max_tokens` limit of the non-streaming helper.

## Try it
```sh
gaia secrets set ANTHROPIC_API_KEY            # once
uv run gaia dev plan <ticket_id> | jq .steps  # prints the plan; the last stdout line is the cost
```
It creates a planning-only run (budget from settings), records the usage against the `lead` agent,
and leaves the run `cancelled` so the orchestrator never resumes it.

## Changing it safely
- **Prompts:** only the lead edits `prompts/*.md`. `test_lead.py` has a golden test that the system
  prompt is exactly the file, so a timestamp slipped in would break caching and fail the test.
- **The output models are a schema contract with the API.** Keep them simple: plain fields and
  lists, no regex constraints, no unions. Add validation as Pydantic validators *after* parsing.
- **Never** put ticket, repo, diff or test-output text outside `wrap_untrusted(...)`.
- New lead calls go through `LeadEngineer`, so refusal and fallback handling stays in one place.

See also: [LLM clients](llm.md) · [security core](security.md) · [review 11](../reviews/2026-11-14-review-11.md).
