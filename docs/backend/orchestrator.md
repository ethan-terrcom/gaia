# The orchestrator: one durable graph per ticket run

> Package **12a** · `gaia/orchestrator/{graph,guard,runner,approvals,deps,state}.py`,
> `gaia/orchestrator/nodes/*.py` · diagram: [`docs/graph.md`](../graph.md) · current as of 2026-11-14

## What it's for
A ticket run is a long workflow with humans in it:

plan → **approve** → implement → check → review (→ revise) → commit → … → final review →
**approve PR** → open PR → watch CI → **approve merge** → merge.

It has to survive a crash or restart in the middle, and wait days at a human gate, without
redoing paid work or creating duplicate approvals. LangGraph runs it as a state machine whose
position is **checkpointed** to SQLite after every step.

12a is the **skeleton**. Every node calls an *interface* (`Planner`, `StepRunner`, `Reviewer`,
`PullRequests`, …); tests satisfy them with fakes, and 12b wires the real ones (11's
`LeadEngineer`, 10b's runtime, 15's GitHub).

## Files
| File | Role |
|---|---|
| `graph.py` | `build_graph(deps, checkpointer=…)`: nodes, edges and **path maps** for every conditional edge |
| `nodes/*.py` | one file per node. Each is thin: load state, call an interface, write rows and events, return a state update |
| `guard.py` | `with_guard`: wraps every node with a terminal check, the budget gate, start and finish events, and failure → `failed` |
| `approvals.py` | `upsert_approval` (idempotent), `Decision` (the resume value), `apply_decision` |
| `runner.py` | `RunManager`: `start`, `resume`, `cancel`, `recover`, the concurrency limit; `open_checkpointer` |
| `deps.py`, `state.py` | the interfaces (`GraphDeps`) and `RunState`, a JSON-safe TypedDict |

## How it works

### Human gates are interrupts
A gate node:
1. **upserts** an `approval` row keyed `"<node>:<step>:<attempt>"`;
2. publishes `approval.requested`;
3. calls `interrupt({...})`.

The graph stops and the checkpoint records where. Later, `RunManager.resume(run_id, decision)`
sends `Command(resume=decision)`, and LangGraph **re-runs the node from its first line**. That is
why step 1 is an upsert, not an insert: on the re-run it finds the existing row, and
`interrupt()` now returns the decision instead of stopping.

**When a run ends early** (cancelled, or failed in a node), its pending approvals are set to
`expired`, and `approval.decided {action: "expired"}` is published, so no inbox item points at a
dead graph. A node guarded on a terminal run returns `{"route": "end"}`, never `{}`: an empty
update keeps the previous node's `route` in state, and the next conditional edge would follow it.

```python
{"action": "approve" | "reject" | "edit", "comment": str | None,
 "plan": dict | None,            # edit only: a replacement plan
 "budget_usd": Decimal | None,   # budget gate only
 "decided_by": str | None}       # filled by the API (doc 14)
```

### The guard around every node
`with_guard` runs before every node:
- **Terminal run:** a run that is already terminal does nothing.
- **Budget:** a run out of budget moves to `paused_budget` and hits a `budget` interrupt.
  - **approve** with a `budget_usd` above what has been spent sets the new cap;
  - **approve** without one grants another `settings.run_budget_usd`;
  - **reject** cancels the run.
- **Events:** emits `run.node_started` and `run.node_finished`.
- **Failures:** any exception except LangGraph's control flow (`GraphBubbleUp`, which covers
  interrupts) marks the run `failed`, emits `run.failed`, and stops the graph.

### Routing is declared
Nodes that branch store `state["route"]` (`"implement_step"`, `"escalation"`, `"end"`, …). Every
`add_conditional_edges` passes a **path map**. Without one, LangGraph silently drops a route to an
unknown node, so a typo would quietly stop a run, and the diagram would show no branches.

### Crashes and restarts
`durability="sync"` makes each step's checkpoint durable before the next runs. At startup,
`RunManager.recover()` examines every run that is not terminal, `awaiting_approval` or
`paused_budget`:
- if its checkpoint holds a pending interrupt, the run is marked `awaiting_approval`;
- otherwise the graph is re-invoked with input `None` on the same thread, which continues from
  the last checkpoint.

The crash-recovery test kills a run mid-step, opens a fresh checkpointer and manager, and shows
the run completing with no duplicate rows.

### Concurrency and safety
- `max_concurrent_runs` (default 3) is an `asyncio.Semaphore`. Waiting runs stay `pending`.
- `resume` refuses a run whose task is still executing (`RunBusy`), because two invocations on one
  checkpoint thread would race.
- `cancel(run_id, workspace_id=…)` is workspace-scoped. Graph errors that aren't `RunFailed` are
  logged, not just stored.

### From parts to a running app (12b)
The graph's nodes call protocols (`deps.py`). Package 12b supplies the real implementations, and
`gaia/orchestrator/wiring.py` assembles them. That file is the **one composition root**, built
once in the app lifespan.

| Protocol | Real implementation | What it does |
|---|---|---|
| `WorktreePreparer` | `impl/worktrees.py` `WorktreeSetup` | creates or reuses the ticket's worktree; runs the repo's setup command in `srt`, with registry-only network |
| `Planner` | `impl/planning.py` `GatewayPlanner` | the lead's plan, through the gateway, on the configured provider |
| `StepRunner` | `impl/steps.py` `RuntimeStepRunner` | runs the step on the role's runtime (`junior_runtime`; `lead_runtime` from attempt `lead_attempt_from`) |
| `CheckRunner` | `impl/checks.py` `SandboxCheckRunner` | the step's test command in `srt`, no network, never asks |
| `Reviewer` | `impl/review.py` `GatewayReviewer` | the lead's step and final reviews |
| `Committer` | `impl/commits.py` `WorktreeCommitter` | `[gaia] <title>`, idempotent |
| `PullRequests` | `services/pull_requests.py` | push, draft PR, CI watch, SHA-pinned merge (15a/15b) |

`build_wiring(...)` also registers both runtimes under their `RuntimeKind` names
(`native_deepseek`, `claude_agent_sdk`). It creates **one** `DbApprovalBroker`, shared by both
runtimes and by the approvals API, because an agent waiting for a command approval is woken
in-process by that exact object. It wraps `srt` lazily, so Gaia starts (and the UI works) even
before `make sandbox-install`.

**Lifespan:** open the checkpointer → `build_wiring` → `build_graph` → reap orphaned agent
processes → `RunManager(...).recover()` → serve. Shutdown runs in reverse. Set
`GAIA_ORCHESTRATOR_ENABLED=false` to run the API without the orchestrator; tests do this.

### The swap drill: proof that Claude is replaceable (ADR-001 P5)
`tests/test_swap_drill.py` runs one ticket from `pending` to `completed` through the **real**
wiring, with `planner_provider=deepseek` and **both** `junior_runtime` and `lead_runtime` on the
native DeepSeek runtime. The provider's HTTP is faked behind the real in-process gateway. The
test asserts:
- every request was DeepSeek `chat/completions`, with zero to Anthropic;
- every usage row is DeepSeek's;
- every token was revoked;
- the commit exists, and the ticket is `done`;
- the Claude runtime was never called: it is patched to fail.

If this test passes, migrating away from Claude is a settings change.

## Changing it safely
- **A new node:** add a file in `nodes/`, `add(...)` it in `graph.py`, declare its routes in a
  path map, add it to the node → status table in doc 12, and regenerate the diagram with
  `make graph`.
- Anything a node does **before** an `interrupt()` must be idempotent: upsert, or check then
  write.
- Keep `RunState` JSON-safe (ids, dicts from `model_dump()`), because it is serialized into the
  checkpoint.
- Don't loosen `RUN_TRANSITIONS` to make a test pass; walk the run through legal statuses instead.

See also: [lead](lead.md) · [runtimes](runtimes.md) · [events](events.md) · [review 12a](../reviews/2026-11-14-review-12a.md).
