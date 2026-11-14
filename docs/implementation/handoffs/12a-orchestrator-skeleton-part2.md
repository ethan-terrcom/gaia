# Handoff: 12a, part 2 — finish the orchestrator skeleton

A previous junior session implemented most of 12a and ran out of turns. **Your job is to finish it,
not to redesign it.** Read the original handoff,
`docs/implementation/handoffs/12a-orchestrator-skeleton.md`, for every rule (sections 2–4 still
apply). Then read the existing code in `backend/gaia/orchestrator/` and
`backend/tests/{orchestrator_fakes,orchestrator_helpers,test_orchestrator_graph,test_orchestrator_nodes}.py`.

Current state, measured by the lead:
- All 8 graph tests pass: happy path, revise and escalation, escalation reject, no duplicate
  approval on resume, budget pause and resume, crash recovery, concurrency, and events.
- 2 node tests fail: `test_open_pr_moves_ticket_to_review` and `test_merge_moves_ticket_to_done`.
  Their setup moves a `pending` run straight to `pr_open`, which `RUN_TRANSITIONS` rightly refuses.
  **Fix the tests' setup**: walk the run through legal statuses, or create the row with the status
  directly through the repo. **Do not loosen `RUN_TRANSITIONS`**, beyond the two F3 transitions
  already added.
- Coverage is 98%. These lines are missed:

  | File | Missed lines |
  |---|---|
  | `gaia/orchestrator/guard.py` | 43-46, 54, 57, 63-75, 98, 155, 158, 159->163, 161, 172, 187-189 |
  | `gaia/orchestrator/runner.py` | 92, 112-124, 139, 145-149, 153, 162->164, 175-176 |
  | `approvals.py` | 39 |
  | `nodes/_common.py` | 27, 37, 54->53 |
  | `nodes/approve_merge.py` | 57 |
  | `nodes/approve_plan.py` | 27->26, 82 |
  | `nodes/approve_pr.py` | 57 |
  | `nodes/escalation.py` | 60 |
  | `nodes/git_commit_step.py` | 24, 27, 40->44 |
  | `nodes/implement_step.py` | 39->43 |

  Write **direct unit tests** that exercise those branches; they are mostly guard and runner error
  paths. Where a branch is genuinely unreachable, delete it rather than adding a pragma, and say so
  in the report.
- Not yet written: `backend/scripts/export_graph.py` and the generated `docs/graph.md` (a title, one
  sentence pointing at ARCHITECTURE §4, and a fenced ```mermaid block from
  `build_graph(...).get_graph().draw_mermaid()`). Build the graph with the test fakes, or with a
  `deps` of stubs; it only needs the topology. Also the `graph` target in the Makefile (that target
  and its `.PHONY` entry only).
- Not yet written: the "Decisions and deviations (as built)" section at the end of
  `docs/implementation/12-orchestrator-graph.md`, marked 12a. It must include:
  - the F3 transitions;
  - the node → status table;
  - each interrupt's idempotency key;
  - the decision shape;
  - anything you or the previous session changed from the handoff.

## Order of work
1. Run `uv run pytest -q tests/test_orchestrator_nodes.py tests/test_orchestrator_graph.py` and fix
   the 2 failing tests.
2. Run `uv run pytest --cov -q` and add tests until coverage of `gaia/` is 100%.
3. Write the export script and generate `docs/graph.md` with `uv run python scripts/export_graph.py`.
4. Add the Makefile target.
5. Write the deviations section.
6. Final checks, from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy` and `uv run pytest --cov -q`, all green.

Do not change production behaviour unless a test exposes a real bug. If one does, describe it under
**Bugs found** in the report. Use the same report headings as the original handoff, plus
`## Bugs found`.
