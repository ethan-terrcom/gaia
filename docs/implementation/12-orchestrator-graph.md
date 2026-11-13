# 12 — Orchestrator Graph (LangGraph)

## Goal
The durable ticket-run workflow from `ARCHITECTURE.md §4`, with human interrupts and
crash recovery.

## Out of scope
HTTP endpoints for approvals (14) and the GitHub PR/merge implementation (15). Stub those
nodes behind an interface for now.

## Files
- `gaia/orchestrator/state.py`: the `RunState` TypedDict
- `gaia/orchestrator/graph.py`: `build_graph(deps) -> CompiledGraph`
- `gaia/orchestrator/nodes/*.py`: one file per node
- `gaia/orchestrator/runner.py`: `RunManager`, which starts, resumes, cancels and lists active runs

## Tasks
1. Add `langgraph` and `langgraph-checkpoint-sqlite`. **Check the APIs against the installed version's
   docs**: `StateGraph`, `START`/`END`, `add_conditional_edges`, `langgraph.types.interrupt` and `Command`,
   and `AsyncSqliteSaver`.
2. Nodes. Each one is idempotent where it can be; on resume, a node may re-run from its start:
   - `prepare_worktree`: create or reuse the worktree (05). Set the run to `planning`.
   - `lead_plan`: call `plan_ticket` (11). Persist `run_step` rows.
   - `approve_plan`: `decision = interrupt({"kind":"plan","plan":...})`. Write an `approval` row
     *before* interrupting. On resume, apply the decision: approve, reject, or approve with an edited plan.
   - `implement_step`: `runtime.run_task(...)` for `steps[step_index]` with the junior runtime. If the
     result is `needs_approval`, call `interrupt({"kind":"command",...})` and then re-dispatch.
   - `run_checks`: run the step's `test_command` through the tool layer.
   - `lead_review_step`: `review_step` (11). Route `accept` → next step or final review; `revise` → `implement_step`
     with feedback (max 3 attempts per step); `escalate` or attempts exhausted → interrupt kind `escalation`.
   - `git_commit_step`: commit after each accepted step with message `[gaia] <step title>`.
   - `lead_final_review` → `approve_pr` (interrupt) → `open_pr` (stub) → `watch_ci` (stub) →
     `approve_merge` (interrupt) → `merge` (stub).
3. Wrap every node in a `with_guard` decorator that:
   - checks the budget (`paused_budget` + interrupt kind `budget` when exceeded),
   - updates `run.status`, emits `run.node_started` and `run.node_finished` events,
   - catches exceptions → `status=failed` + an event. It never swallows `GraphInterrupt`.
4. `RunManager`:
   - `start(ticket_id)` creates a `run` row and launches `graph.ainvoke(initial_state, {"configurable":{"thread_id": run_id}})` as an `asyncio.Task`.
   - `resume(run_id, decision)` → `graph.ainvoke(Command(resume=decision), config)`.
   - On app startup, find runs in non-terminal, non-awaiting states and resume them from their checkpoint.
   - Concurrency limit: `settings.max_concurrent_runs` (default 3).
5. Write the graph's Mermaid diagram to `docs/graph.md` via `graph.get_graph().draw_mermaid()`.

## Tests (FakeLLM + fake runtime)
- Happy path: plan → approve → 2 steps → PR approval → done.
- Revise loop, then escalation after 3 failures.
- Budget exceeded pauses the run.
- **Crash recovery:** kill the task mid-`implement_step`, rebuild the graph from the same checkpoint
  file, resume, and the run completes.

## Acceptance criteria
- [ ] All the graph tests above pass. `docs/graph.md` is generated.
