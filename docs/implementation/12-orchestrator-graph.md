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

## Decisions and deviations (as built) — 12a

- **F3 transitions added:** `RUN_TRANSITIONS` now allows `PENDING → PAUSED_BUDGET`
  and `PLANNING → PAUSED_BUDGET`, because `with_guard` checks the budget before
  every node. `failed` remains terminal: a retry is a new run.
- **Node → run status table:**

  | Node | `run.status` |
  |---|---|
  | `prepare_worktree`, `lead_plan` | `planning` |
  | `approve_plan`, `approve_pr`, `approve_merge`, `escalation`, and every interrupt | `awaiting_approval` |
  | `implement_step`, `run_checks`, `git_commit_step` | `implementing` |
  | `lead_review_step`, `lead_final_review` | `reviewing` |
  | `open_pr`, `watch_ci`, `merge` | `pr_open` |
  | `finish` | `completed` |

- **Interrupt idempotency keys:**
  - `approve_plan`: `approve_plan:<step_index>:<attempt>`
  - `escalation`: `escalation:<step_index>:<attempt>`
  - `approve_pr`: `approve_pr:<step_index>:<attempt>`
  - `approve_merge`: `approve_merge:<step_index>:<attempt>`
  - budget guard: `budget:<node_name>:<run.cost_usd>`
- **Decision shape:** `{"action": "approve" | "reject" | "edit", "comment":
  str | None, "plan": dict | None, "budget_usd": Decimal | None, "decided_by": str | None}`, validated by
  `gaia.orchestrator.approvals.Decision`. `edit` requires `plan`.
- **Deviations from the handoff:**
  - The four node approval paths and the budget guard no longer contain a
    defensive `if approval_row is None: raise ValueError` after re-loading the
    approval row. The row is always created by the idempotent upsert immediately
    before the interrupt, so that branch was unreachable; it was deleted instead
    of adding coverage pragmas.
  - `RunState` carries one extra JSON-safe key, `route`, so conditional edges can
    route without reaching into the database. It uses the default last-value-wins
    reducer, so no reducer was added.
  - `docs/graph.md` is generated with a dummy `GraphDeps` object rather than the
    test fakes: the diagram only needs the compiled graph topology, and the
    dependencies are not executed during `draw_mermaid()`.
  - `RunManager.start` creates `budget_usd=Decimal(str(settings.run_budget_usd))`
    and `graph_thread_id=run_id`; both are required by the 12a handoff notes.

### Lead review changes — 12a (2026-11-14, [`review-12a`](../reviews/2026-11-14-review-12a.md))
- **R5:** every conditional edge has an explicit path map. An unknown route now raises instead of
  silently ending the run, and `docs/graph.md` shows all 28 edges (it showed 4).
- **G1:** budget "approve" without `budget_usd` grants another `settings.run_budget_usd`. An
  explicit cap must exceed what has been spent.
- **R1–R4:** `resume` refuses a busy run (`RunBusy`); graph errors are logged; `cancel` is
  workspace-scoped; finished tasks are pruned.
- **A1:** `Decision.decided_by` is recorded on the approval row.
- `domain/plans.py`: 11's models are kept, plus the empty-title check.

- [x] Acceptance (12a part): all the graph tests above pass, and `docs/graph.md` is generated (`make graph`).

## Decisions and deviations (as built) — 12b-2

- **Provider is settings, never graph code (ADR-001 P2).** `Settings.planner_provider`,
  `planner_model` and `lead_call_timeout_s` are the only lead-side provider/model knobs.
  `gaia/orchestrator/` imports neither `anthropic` nor `openai`.
- **`gaia/llm/providers.py` is the composition seam.** `build_structured_llm` dispatches by
  provider name to the two adapter classes; the orchestrator imports only this factory.
- **`AnthropicClient` gained `base_url` and `aclose`.** The gateway route is
  `http://<host>:<port>/llm/anthropic`, and the installed anthropic SDK appends `/v1/messages`.
  `aclose` closes the underlying async client when it has a `close` method.
- **`lead_llm` mints a run token per call and revokes it in `finally`.** The client recorder is
  10b's in-memory `UsageTally`, never a DB writer, so the gateway remains the only writer of
  `llm_usage` rows (P4). The token TTL is `lead_call_timeout_s + 300`.
- **`GatewayPlanner` and `GatewayReviewer` hold no DB session during an LLM call.** They load
  their rows in a short `session_scope`, close it, then enter `lead_llm`.
- **The reviewer rebuilds the plan from rows.** `plan_from_rows(steps, approval_payload)` takes
  `PlanStep`s from `run_step.spec_json` (ordered by `step_index`) and `summary`/`risks` from the
  most recent plan approval's `payload_json["plan"]`, falling back to `""` and `[]`. "Most
  recent" sorts by `created_at`, because UUIDv7 ids are not monotonic within one millisecond.
- **Final review diff base.** `GatewayReviewer.final_review` runs
  `GitService.diff(WorktreeLocation, worktree.base_sha, "HEAD")`, deriving `git_dir` from the
  trusted repository when a worktree row predates that column.
- **Portability test (P5/P6) falls back to stub clients for the two-provider drill.** Wiring the
  anthropic SDK's streamed structured-output parser to the in-process ASGI app was not needed for
  this package; the drill uses a provider-neutral `StructuredLLM` stub for both providers. One
  gateway-path test drives the real `DeepSeekClient` through the app gateway and asserts a single
  gateway-written `llm_usage` row, an increased `run.cost_usd`, and a revoked run token.

### Lead review — 12b-2 (2026-11-14, [`review-12b2`](../reviews/2026-11-14-review-12b2.md))
- No code changes. The junior's gateway-path test covers DeepSeek only, so the default provider
  (Claude) was **verified live** instead. `GatewayPlanner.plan` and `GatewayReviewer.review_step`
  ran through the real gateway on both providers. Each returned a valid plan, and a `revise`
  verdict that asked for the missing test in a deliberately incomplete diff. Each call wrote
  exactly one gateway `llm_usage` row, and every run token was revoked:
  - Claude Opus 5.5: $0.045 for the plan and the review;
  - DeepSeek v4 Pro: $0.005.

  Neither provider key appears in the server log.
