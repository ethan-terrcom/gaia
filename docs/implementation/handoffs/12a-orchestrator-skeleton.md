# Handoff: 12a — Orchestrator skeleton on fakes

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs.
The lead does the commit and merge, and the report goes through `finish`. 12a is the riskiest
component, so the lead **pair-reviews it line by line**. Prefer obvious code over clever code.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **12a**: the durable LangGraph workflow for a ticket run, with
every node calling **interfaces** that tests satisfy with fakes. 12b wires the real services later.
The lead engineer reviews your work, then commits and merges it. You never commit.

`langgraph==1.2.19`, `langgraph-checkpoint==4.3.1` and `langgraph-checkpoint-sqlite==3.1.3` are
**already installed and pinned** on this branch. Read their source under
`backend/.venv/lib/python3.12/site-packages/langgraph/` for every API you use.

## 1. Read first, in this order
1. `docs/implementation/12-orchestrator-graph.md`: your contract (the 12a part, tasks 1–5, Tests).
2. `docs/ARCHITECTURE.md` §4, every bullet. They are requirements.
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/implementation/11-lead-planning-and-review.md`, "Output models". Package 11 is being built
   in parallel (see the notes on `plans.py` below).
5. `docs/backend/README.md`, then `docs/backend/database.md`, `events.md` and `python-concepts.md`.
6. The code you build on:
   - `gaia/domain/{enums,state}.py` (`transition`, and the run, step and ticket tables);
   - `gaia/db/{models,repo,engine}.py` (`Run`, `RunStep`, `Approval` with
     `UNIQUE(run_id, idempotency_key)`);
   - `gaia/services/{events,costs}.py` (`EventBus`, `BusEventSink`, `EventSink`,
     `remaining_budget`);
   - `gaia/config/settings.py` (`checkpoint_path`, `run_budget_usd`);
   - `tests/conftest.py`, `tests/db_builders.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**Verified LangGraph 1.2.19 facts** (the lead checked the installed source; confirm them as you use
them):
- `from langgraph.graph import StateGraph, START, END`; the compiled type is
  `langgraph.graph.state.CompiledStateGraph`.
- `from langgraph.types import interrupt, Command`. A node calls `decision = interrupt(payload)`.
  The caller resumes with `await graph.ainvoke(Command(resume=decision), config)`.
- `langgraph.errors.GraphBubbleUp` is the base class of `GraphInterrupt` and `ParentCommand`.
  `with_guard` must re-raise **`GraphBubbleUp`**, not only `GraphInterrupt`, and must never catch
  `BaseException` (`asyncio.CancelledError` must propagate).
- `ainvoke(..., durability="sync")` is a parameter of `ainvoke` and `astream` (default `"async"`).
  Pass `durability="sync"` on **every** call.
- `AsyncSqliteSaver.from_conn_string(path)` (in `langgraph.checkpoint.sqlite.aio`) is an **async
  context manager**. `await saver.setup()` creates its tables.
- `graph.get_graph().draw_mermaid()` renders the diagram, and `await graph.aget_state(config)`
  returns the checkpoint's `StateSnapshot`. Check its fields for pending interrupts (`.interrupts`
  or `.tasks[*].interrupts`) in the installed source.

**`gaia/domain/plans.py`**
- Create it with **exactly** doc 11's three models (`PlanStep`, `Plan`, `Review`), plus these
  validators:
  - `Plan.steps` has 1–10 items;
  - `PlanStep.title` is at most 200 characters, and `instructions` 1–20,000;
  - `test_command` is `None` or 1–64 items, each 1–1000 characters, with no NUL.
- Package 11 creates the same file in parallel. The lead keeps 11's version at merge, so do not add
  anything else to it.

**State (`gaia/orchestrator/state.py`)**
- `RunState(TypedDict, total=False)` holds only JSON-safe values: `run_id`, `ticket_id`, `repo_id`,
  `workspace_id`, `worktree_id: str | None`, `plan: dict | None` (a `Plan.model_dump()`),
  `step_index: int`, `attempt: int`, `feedback: str | None`, `step_results: list[dict]`,
  `review: dict | None`, `pr_number: int | None`, `error: str | None`.
- Use no reducers unless you need one. Explain any you add.

**Dependencies are interfaces (`gaia/orchestrator/deps.py`)**
- These are `Protocol`s, each with exactly the methods the nodes call:
  - `WorktreePreparer.prepare(run_id) -> str` (returns the worktree id);
  - `Planner.plan(run_id) -> Plan`;
  - `StepRunner.implement(run_id, step_index, attempt, feedback) -> StepOutcome`
    (`status`, `summary`, `files_changed`, `diff`);
  - `CheckRunner.run(run_id, step_index) -> CheckOutcome` (`passed`, `output`);
  - `Reviewer.review_step(run_id, step_index, outcome, checks) -> Review`;
  - `Reviewer.final_review(run_id) -> Review`;
  - `Committer.commit_step(run_id, step_index, title) -> str` (returns the sha);
  - `PullRequests.open_pr(run_id) -> int`, `PullRequests.watch_ci(run_id, pr) -> str`
    (`"passed"` or `"failed"`) and `PullRequests.merge(run_id, pr) -> None`.
- `GraphDeps` is a frozen dataclass of all of the above, plus `sessions`, `events: EventBus` and
  `settings`.
- Fakes live in `tests/orchestrator_fakes.py`, with scriptable outcomes and call recording. **Not**
  in `gaia/`.

**Nodes (`gaia/orchestrator/nodes/*.py`, one per node)** follow ARCHITECTURE §4 and task 2.
- The `run.status` each node sets:

  | Node | `run.status` |
  |---|---|
  | `prepare_worktree`, `lead_plan` | `planning` |
  | `approve_plan`, and every interrupt | `awaiting_approval` |
  | `implement_step`, `run_checks`, `git_commit_step` | `implementing` |
  | `lead_review_step`, `lead_final_review` | `reviewing` |
  | `open_pr`, `watch_ci`, `merge` | `pr_open` |
  | the end | `completed` |

  Use `transition()`. A move to the current status is a no-op.
- The ticket moves `ready → in_progress` on the first `implement_step` (a `backlog` ticket goes
  through `ready` first), to `in_review` when the PR opens, and to `done` on merge.
- **`lead_plan`** persists `run_step` rows **idempotently**: if rows already exist for the run, do
  nothing. It stores `plan` in the state.
- **Every interrupting node** follows the same pattern:
  1. upsert an `approval` row with `idempotency_key=f"{node}:{step_index}:{attempt}"` and the
     payload, then publish `approval.requested`;
  2. **then** call `interrupt({"kind": …, "approval_id": …, …})`;
  3. on resume, update the row's status from the decision, then publish `approval.decided`.

  The node re-runs from its first line on resume, so step 1 must be an upsert: the
  `IntegrityError` on the unique key, or a select-first, is how you prove it.
- **Decision shape:** `{"action": "approve" | "reject" | "edit", "comment": str | None, "plan":
  dict | None}`. Validate it with a Pydantic model.
  - Plan rejection ends the run as `cancelled`.
  - `edit` replaces the plan (re-validated through `Plan`) and re-persists the steps: delete the
    pending ones, then insert the new ones.
  - Escalation approve means one more attempt; reject means `failed`.
  - `pr_open` and `merge` reject: the run stays as it is and ends `cancelled`.
- **Routing:**
  - `lead_review_step`: `accept` goes to `git_commit_step`, then the next step or
    `lead_final_review`.
  - `revise` goes back to `implement_step` with `attempt + 1` and `feedback`, up to **3 attempts**
    per step.
  - `escalate`, or attempts exhausted, goes to an `escalation` interrupt.
- `open_pr`, `watch_ci` and `merge` call `PullRequests`. A `watch_ci` result of `failed` goes to an
  `escalation` interrupt.

**`with_guard` (`gaia/orchestrator/guard.py`)** is a decorator that wraps every node.
1. Load the run; if it is terminal, return `{}`.
2. **Budget:** if `remaining_budget(run) <= 0`:
   - transition to `paused_budget`;
   - upsert the budget approval with key `budget:{node}:{run.cost_usd}`;
   - call `interrupt({"kind": "budget", …})`.

   On resume:
   - `approve` with a new `budget_usd` in the decision: update the run and continue with the node;
   - `reject`: the run is `cancelled`.
3. Emit `run.node_started`, run the node, then emit `run.node_finished`.
4. Re-raise `GraphBubbleUp`. For any other `Exception`:
   - transition the run to `failed` and set `state.error` to the exception class plus a redacted
     message;
   - emit `run.failed`;
   - raise `RunFailed` (your own exception) so the graph stops.

**F3 (review 01-05, assigned to 12a):** the guard checks the budget before *every* node, so add
`PENDING → PAUSED_BUDGET` and `PLANNING → PAUSED_BUDGET` to `RUN_TRANSITIONS`, with tests. **Keep
`failed` terminal**: a retry is a new run (doc 14). Record this in the deviations section.

**`RunManager` (`gaia/orchestrator/runner.py`)**
- `__init__(self, graph, deps, *, max_concurrent_runs=settings.max_concurrent_runs)`. Add
  `Settings.max_concurrent_runs: int = Field(default=3, ge=1)`.
- `async start(ticket_id, *, workspace_id, created_by) -> str`:
  - creates the run with `budget_usd=Decimal(str(settings.run_budget_usd))` (**required**: the
    default `0` makes the 07b gateway refuse every call) and `graph_thread_id=run_id`;
  - launches `graph.ainvoke(initial_state, config, durability="sync")` as an `asyncio.Task`,
    gated by an `asyncio.Semaphore`. A run waiting for a slot stays `pending`.
- `async resume(run_id, decision)`: `Command(resume=decision)` in a new task.
- `async cancel(run_id)`: cancel the task, transition the run to `cancelled`, emit the event.
- `active() -> list[str]`.
- `async recover()`, called at startup. For runs whose status is not terminal, `awaiting_approval`
  or `paused_budget`:
  - read `aget_state`;
  - if it has a pending interrupt, set the run to `awaiting_approval` and leave it;
  - otherwise resume with input `None` on the same thread.
- `async aclose()`: cancel and await every task.
- `open_checkpointer(settings) -> AsyncContextManager[AsyncSqliteSaver]` wraps `from_conn_string`
  plus `setup()`. The app lifespan wiring is **12b**; don't edit `api/app.py`.

**Graph diagram:** `backend/scripts/export_graph.py` writes `docs/graph.md`: a title, a sentence
pointing at ARCHITECTURE §4, and a fenced ```mermaid block from `draw_mermaid()`. Add a `graph`
target to the Makefile (that target and its `.PHONY` entry only), and commit the generated
`docs/graph.md`.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/orchestrator/**`, except `prompts/` (package 11's);
- `backend/gaia/domain/plans.py` (as above) and `backend/gaia/domain/state.py` (the F3 transitions
  only);
- `backend/gaia/config/settings.py` (`max_concurrent_runs`);
- `backend/scripts/export_graph.py`, `docs/graph.md`, and the `graph` Makefile target;
- `backend/tests/**`;
- a "Decisions and deviations (as built)" section at the end of
  `docs/implementation/12-orchestrator-graph.md`, marked "12a".

**You must not:**
- edit `pyproject.toml` or `uv.lock` (the deps are already there), other docs, `.github/`,
  `gaia/api/**`, `gaia/llm/**`, `gaia/security/**` or `gaia/runtimes/**`;
- add migrations;
- call real LLMs, git or GitHub.

## 4. Non-negotiable rules
- Every side effect before an `interrupt()` is idempotent. Prove it with the "resume does not
  duplicate the approval row" test.
- SQL only through `gaia/db/` (`ScopedRepo`, or a small repo method you add there; you may edit
  `gaia/db/repo.py` for that).
- No blocking calls on the event loop. Tests clean up tasks and close savers; warnings are errors.
- `durability="sync"` on every graph call. The checkpoint DB file comes from
  `settings.checkpoint_path`, or `tmp_path` in tests.

## 5. How to work
1. Plan first: the node list with edges, the `RunState` keys, the interfaces, and the test list.
2. Build in this order: `plans.py`, then the state, then the deps protocols plus test fakes, then
   `with_guard`, then the nodes one at a time (each with a unit test calling the node function
   directly), then `build_graph`, then `RunManager`, then the graph tests, then the diagram.
3. **Required graph tests** (FakeLLM is not needed; the fakes are the interfaces), each against a
   real `AsyncSqliteSaver` on a temp file:
   - happy path: plan, approve, 2 steps, PR approval, merge approval, `completed`, ticket `done`;
   - a revise loop, then an escalation interrupt after 3 failed attempts (approve gives one more
     attempt; reject gives `failed`);
   - resuming an interrupted plan approval does **not** duplicate the `approval` row;
   - an exhausted budget pauses the run (`paused_budget`, a `budget` interrupt), and resuming with
     a raised budget continues;
   - **crash recovery:**
     - the fake `StepRunner` blocks on an `asyncio.Event` mid-`implement_step`;
     - cancel the task (the "crash"), close the saver;
     - open a **new** saver on the same file, build a **new** graph and a new `RunManager`;
     - `recover()` resumes the run and it completes;
     - `implement` ran twice for that step, and there are no duplicate `run_step` or `approval`
       rows;
   - `with_guard`: an exception in a node gives `failed` plus a `run.failed` event, and an
     interrupt is not swallowed;
   - concurrency: with `max_concurrent_runs=1`, the second run waits as `pending` until the first
     finishes.
4. Before you finish, all of these must pass, run from `backend/`: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q` (100% coverage of
   `gaia/`). Also run `uv run python scripts/export_graph.py`.
5. Write the "Decisions and deviations (as built)" section.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Graph   (node list, edges/routing, which nodes interrupt and their idempotency keys)
## Tests in the doc   (each, with the test name that proves it)
## LangGraph APIs used   (symbol, installed source file checked)
## Deviations from the doc   (include the F3 transitions)
## Security-relevant changes
## Tests added
## TODOs left for 12b/14/15
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
