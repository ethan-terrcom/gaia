# Agent runtimes: the seam and its shared machinery

> Package **10a** · `gaia/runtimes/{base,support,approvals,orphans,fake,fake_agent}.py`,
> `services/proc.py` (`stream_proc`), `GitService.changed_paths` · current as of 2026-11-14

## What it's for
A **runtime** is whatever actually runs an agent on a task:
- a native tool loop over DeepSeek (10b);
- the Claude Agent SDK (10c);
- in tests, a fake.

ADR-001 decides which ones ship. 10a builds what they **all** share, so no runtime re-implements
timeouts, cancellation, `files_changed`, environment scrubbing, approvals or orphan cleanup, and so
no runtime can get them wrong.

```python
class AgentRuntime(Protocol):
    name: str
    async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult: ...
    async def cancel(self, task_id: str) -> None: ...
```

Callers never call `run_task` directly. They call:

```python
result = await run_with_limits(runtime, task, BusEventSink(bus, ...), git=git)
```

## Files
| File | Role |
|---|---|
| `base.py` | `AgentTask` (worktree as a pinned `WorktreeLocation`, `base_sha`, timeout, `extra_allow`), `AgentResult`, `RuntimeUsage`, the `AgentRuntime` protocol, `RuntimeRegistry` |
| `support.py` | `run_with_limits` (timeout, outer cancellation, failure → result, git-computed `files_changed`), `build_agent_env`, `files_changed` |
| `approvals.py` | `ApprovalBroker`: an agent asks, the runtime **waits inside the turn**. `InMemoryApprovalBroker` (tests) and `DbApprovalBroker` (approval row plus events) |
| `orphans.py` | `OrphanRegistry`: pids of runtime process groups in a 0600 JSON file, reaped at startup only when verified |
| `fake.py`, `fake_agent.py` | `FakeRuntime` (in-process, scripted) and `FakeProcessRuntime` (a real subprocess speaking JSON lines) |
| `services/proc.py` | `stream_proc`: `run_proc` that streams lines to a callback, for harness runtimes |

## How it works

### The limits every runtime gets
`run_with_limits`:
1. emits `agent.message`;
2. runs the task under `asyncio.timeout(task.timeout_s)`;
3. on **timeout**: asks the runtime to cancel, waiting at most `CANCEL_GRACE_S`, and returns
   `status="timeout"`;
4. on an **outer cancel** (the run was cancelled): cancels the runtime the same bounded way, and
   re-raises;
5. on **any other exception**: returns `status="failed"` with a short, redacted error;
6. always computes `files_changed` from git (`git diff --name-only <base>` plus untracked files),
   never from what the agent claims, and emits `agent.usage`.

### The agent's environment
`build_agent_env` builds the dict **from nothing**: `PATH`, `HOME` and `TMPDIR` (both the task's
temp dir) and `LANG`. When the task talks to a model, it adds the gateway URL and a `gaia_rt_` run
token as `ANTHROPIC_*` or `OPENAI_*`. It never reads `os.environ`. A test plants a fake key in the
parent environment and proves it never reaches the child.

### Approvals inside the turn
When the command policy says `ask`, the runtime calls
`await broker.request(task, kind=COMMAND, payload={"argv": …}, timeout_s=…)`. `DbApprovalBroker`
then:
1. **reuses** the row for an identical request in the same task. An approved or rejected answer
   comes back at once, so a refused command stays refused. Only an expired row is asked again;
2. registers its waiter, **then** publishes `approval.requested`. The other order would lose a
   decision that arrived while the publish was in flight;
3. waits. A timeout means **deny**, and the row becomes `expired`;
4. returns what the **row** says, so a decision that raced the timeout still wins.

`decide(...)`, which doc 14's route will call, refuses a row that is no longer pending
(`ApprovalNotPending`), so a late click can't rewrite an expired approval.

### Orphans
A runtime registers every process group it starts. At startup, `reap_stale` kills an entry's group
only if `ps` confirms that the pid still has the recorded start time **and** command. pids get
reused, and killing a stranger's process is worse than leaving an orphan.

## Try it
```sh
git -C <repo> worktree add <wt> -b gaia/try main
GAIA_DATA_DIR=/tmp/g uv run gaia dev run-task --runtime fake --worktree <wt> "add hello()"
# → {"status": "completed", "files_changed": ["hello.py"], ...}  (logs go to stderr)
```

## Changing it safely
- **A new runtime:**
  - implement `AgentRuntime`;
  - add it to the contract-suite parametrisation in `tests/test_runtime_contract.py`;
  - make every contract test pass. That suite *is* the definition of "a runtime".
- **Don't** handle timeouts or `files_changed` inside a runtime; `run_with_limits` owns them.
- **Don't** spawn processes outside `services/proc.py` (ruff `TID251`).
- Every `await` on the cancel or timeout path must be bounded.

See also: [events](events.md) · [gateway](gateway.md) · [security core](security.md) ·
[ADR-001](../decisions/ADR-001-agent-runtimes.md) · [review 10a](../reviews/2026-11-14-review-10a.md).
