# 12 — Orchestrator Graph (LangGraph)

**Split (2026-11-12 review):** **12a** is the graph skeleton on fakes: `RunState`, the checkpointer,
every node as a stub, the interrupts, `with_guard`, `RunManager` and the crash-recovery test. It
needs only doc 03, so it can start early. Lead-owned or pair-reviewed. **12b** wires the real nodes
to the services and runtimes (10b, 11).

## Goal
The durable ticket-run workflow from `ARCHITECTURE.md §4`, with human interrupts and
crash recovery.

## Out of scope
HTTP endpoints for approvals (14) and the GitHub PR/merge implementation (15). Stub those
nodes behind an interface for now.

## Files
- `gaia/orchestrator/state.py`: the `RunState` TypedDict
- `gaia/orchestrator/graph.py`: `build_graph(deps) -> CompiledStateGraph`
- `gaia/orchestrator/nodes/*.py`: one file per node
- `gaia/orchestrator/runner.py`: `RunManager`, which starts, resumes, cancels and lists active runs

## Tasks
1. Add `langgraph` and `langgraph-checkpoint-sqlite`. **Check the APIs against the installed version's
   docs**: `StateGraph`, `START`/`END`, `add_conditional_edges`, `langgraph.types.interrupt` and `Command`,
   and `AsyncSqliteSaver`. At the time of writing, LangGraph is 1.2: the compiled type is `CompiledStateGraph`,
   `AsyncSqliteSaver.from_conn_string()` is an async context manager held open for the app's lifespan,
   and graph calls take an explicit `durability="sync"`.
2. Nodes. Each one is idempotent where it can be; on resume, a node may re-run from its start:
   - `prepare_worktree`: create or reuse the worktree (05), then run the repo's owner-registered
     `setup_command` in the sandbox. Set the run to `planning`.
   - `lead_plan`: call `plan_ticket` (11). Persist `run_step` rows.
   - `approve_plan`: `decision = interrupt({"kind":"plan","plan":...})`. **Upsert** the `approval` row
     (keyed by run, node, step and attempt) before interrupting. The node re-runs from its first line on
     resume, so a plain insert would duplicate it. On resume, apply the decision: approve, reject, or
     approve with an edited plan.
   - `implement_step`: `runtime.run_task(...)` for `steps[step_index]` with the junior runtime. Command
     approvals are awaited inside the runtime (doc 10, requirement 8); this node never interrupts for
     them. Move the ticket to `in_progress` on the first step.
   - `run_checks`: run the step's `test_command` through the tool layer.
   - `lead_review_step`: `review_step` (11). Route `accept` → next step or final review; `revise` → `implement_step`
     with feedback (max 3 attempts per step); `escalate` or attempts exhausted → interrupt kind `escalation`.
   - `git_commit_step`: commit after each accepted step with message `[gaia] <step title>`.
   - `lead_final_review` → `approve_pr` (interrupt) → `open_pr` (stub) → `watch_ci` (stub) →
     `approve_merge` (interrupt) → `merge` (stub). Move the ticket to `in_review` when the PR
     opens and to `done` on merge.
3. Wrap every node in a `with_guard` decorator that:
   - checks the budget (`paused_budget` + interrupt kind `budget` when exceeded),
   - updates `run.status`, emits `run.node_started` and `run.node_finished` events,
   - catches exceptions → `status=failed` + an event. It re-raises LangGraph's control-flow exceptions
     (`GraphInterrupt` and its base class; check the installed version) and catches only the rest.
4. `RunManager`:
   - `start(ticket_id)` creates a `run` row and launches `graph.ainvoke(initial_state, {"configurable":{"thread_id": run_id}})` as an `asyncio.Task`.
   - `resume(run_id, decision)` → `graph.ainvoke(Command(resume=decision), config)`.
   - On app startup, find runs in non-terminal, non-awaiting states and resume them from their checkpoint
     (invoke with input `None` on the same `thread_id`). Reconcile `run`/`run_step` rows from the
     checkpoint, which is the source of truth.
   - Concurrency limit: `settings.max_concurrent_runs` (default 3).
5. Write the graph's Mermaid diagram to `docs/graph.md` via `graph.get_graph().draw_mermaid()`.

## Tests (FakeLLM + fake runtime)
- Happy path: plan → approve → 2 steps → PR approval → done.
- Revise loop, then escalation after 3 failures.
- Resuming an interrupted plan approval does not duplicate the `approval` row.
- Budget exceeded pauses the run.
- **Crash recovery:** kill the task mid-`implement_step`, rebuild the graph from the same checkpoint
  file, resume, and the run completes.

## Acceptance criteria
- [ ] All the graph tests above pass. `docs/graph.md` is generated.
