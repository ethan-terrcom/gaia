# Agent runtimes: the seam and its shared machinery

> Packages **10a** (the seam) and **10b** (the native runtime) ·
> `gaia/runtimes/{base,support,approvals,orphans,fake,fake_agent,finish,native,native_llm}.py`,
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

### Which model a task runs on (19b)
`AgentTask.model` names the model; `None` means the runtime's own default (the only behaviour
before 19b). The native runtime's `gateway_llm_factory` takes the task model's provider from the
catalog (DeepSeek, OpenAI or xAI) and refuses an Anthropic model; the Claude runtime passes the
task model to the SDK and prices usage at it. The orchestrator picks the runtime from the model's
provider (`services/agent_modes.runtime_for_model`), and the native task's auto-mode switch is
its own model's family.

## The native runtime (10b): Gaia's own agent loop
`NativeRuntime` is the default junior runtime (ADR-001). It is a plain loop that Gaia owns end to
end, and it is **provider-neutral**: it talks to any `ToolChatLLM` (`gaia/llm/base.py`), and
DeepSeek is simply the first implementation.

```
run_task(task)                                   ← runs the loop in its own child task
  handle = llm_factory(task)        mint a run token, client → gateway /llm/deepseek
  ctx    = context_factory(task)    09b ToolContext: sandbox runner, broker, audit
  messages = [system: junior_system.md, user: task.instructions]
  repeat up to task.max_turns:
    resp = llm.complete(messages, tools = registry.specs() + finish)
    emit agent.message / agent.usage
    append the assistant message (with reasoning_content: DeepSeek requires it)
    no tool calls 3 turns running → failed
    each tool call:
      finish  → validate FinishArgs → completed (or feed the error back)
      other   → registry.call(...) → append the tool result (every call gets one)
  → failed: max turns
finally: close the handle (revokes the token)
```

- **Cost and budget live at the gateway.** The client's recorder is an in-memory `UsageTally`; the
  gateway writes the one `llm_usage` row and updates `run.cost_usd`. A refused call (403
  `budget_exceeded`) becomes `LLMBudgetExceeded` in the adapter, and `budget_exceeded` here.
- **History is append-only** (requirement 10). Messages are frozen dataclasses in a list that is
  only appended to.
- **`finish`** (`gaia/runtimes/finish.py`) is the one tool the registry does not own. It ends the
  task and never touches the worktree. The Claude Agent SDK runtime offers the same tool with the
  same schema, so one prompt works for both.
- **Cancellation:** `cancel(task_id)` stops only the agent's loop, and `run_task` returns
  `cancelled`. Cancelling the *caller* propagates as usual. The loop runs in a child task for
  exactly this reason: registering the caller's task would let `cancel()` tear down the graph
  node that called it.

### Portability (ADR-001)
- **P1:** `tests/test_vendor_boundaries.py` walks every import under `gaia/`: `anthropic` only in
  `llm/anthropic_client.py`, `openai` only in `llm/openai_compat_client.py`, `claude_agent_sdk` only
  in `runtimes/claude_agent_sdk.py`.
- **P6:** `DeepSeekClient.parse` implements `StructuredLLM`. DeepSeek has no JSON-schema mode, so
  it uses `response_format=json_object` with the schema in the system prompt, plus one repair
  call. `tests/test_lead_portability.py` runs the lead's planning and review on it, and it was
  verified live: DeepSeek planned a real ticket through the gateway.

## The Claude Agent SDK runtime (10c): Claude as a thin adapter
`ClaudeAgentSdkRuntime` runs the lead role (ADR-001). Claude Code's agent loop is excellent, but
the owner's condition was that Gaia must be able to walk away from it. So the SDK supplies **only
the loop and the model**; everything else is Gaia's:

| Concern | How 10c keeps it Gaia's |
|---|---|
| Tools (P3) | `tools=[]` turns every built-in tool off, plus a `disallowed_tools` list. Gaia's 09b registry is served as one in-process MCP server, `gaia`, and `allowed_tools` is the exact `mcp__gaia__*` list. A `PreToolUse` hook denies anything else |
| Policy, approvals, audit, events | the same `registry.call` the native loop uses |
| Configuration | `setting_sources=[]` (no `.claude/`, `CLAUDE.md` or user settings), `strict_mcp_config=True` (no repo `.mcp.json`), and Gaia's own Markdown system prompt, never the `claude_code` preset |
| State and cost (P4) | no SDK sessions, `resume` or checkpointing. Usage is the result's token counts priced by Gaia, never `total_cost_usd`. A test greps the module for the banned names |
| Credentials | a per-task run token; the gateway holds the real key |
| Environment and sandbox | see the wrapper, below |

**The wrapper.** The SDK merges its `env` option onto Gaia's own environment, so the CLI is never
started directly. `cli_path` points at a per-task script that:
1. writes its pid, for orphan cleanup;
2. `env -i` rebuilds the environment from nothing (`PATH`, `HOME`/`TMPDIR` = the task dir, the
   gateway URL, the run token renamed to `ANTHROPIC_API_KEY`, `MCP_TOOL_TIMEOUT`);
3. enters `srt` (network only to the gateway), then `env -u NO_PROXY` so traffic uses srt's proxy;
4. execs the SDK's bundled CLI.

The wrapper and the srt settings live in a control dir under `{data_dir}/sandbox/`, which the
CLI cannot write.

**Two timeouts.** The CLI enforces its own per-tool-call timeout (`MCP_TOOL_TIMEOUT`, verified
live to be honoured). One Gaia tool call can wait for an approval and then run a command for up to
30 minutes, so the value is the approval timeout + 1,800 s + 120 s.

**What migrating away costs:** this one file. Nothing else in Gaia imports the SDK
(`tests/test_vendor_boundaries.py`), and the same contract suite proves a replacement.

## Try it
```sh
git -C <repo> worktree add <wt> -b gaia/try main
GAIA_DATA_DIR=/tmp/g uv run gaia dev run-task --runtime fake --worktree <wt> "add hello()"
# → {"status": "completed", "files_changed": ["hello.py"], ...}  (logs go to stderr)
```
The native runtime, against a running server (`gaia serve` or the dev server) that holds a
DeepSeek key, with srt installed (`make sandbox-install`):
```sh
uv run gaia dev run-task --runtime native --gateway-base http://127.0.0.1:8765 \
  --worktree <a gaia worktree> "Add farewell(name) to src/greet.py with a test, then finish"
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
