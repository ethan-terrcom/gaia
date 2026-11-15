# Handoff: 10c — Claude Agent SDK runtime (Gaia tools only)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is to implement exactly one work package, **10c**: `ClaudeAgentSdkRuntime`, the runtime ADR-001
chose for **lead implementation work and escalations**. It drives the Claude Agent SDK
(`claude-agent-sdk==0.2.175`, already installed by the lead).

**The owner's condition (read this twice).** The owner accepted Claude for this role on one
condition: Gaia must be able to migrate away from it later without a rewrite. So this runtime is a
**thin adapter**:
- the SDK supplies the agent loop and the model, **nothing else**;
- tools, policy, approvals, audit, events, environment, sandbox, state and cost all stay
  Gaia's.

The rules are ADR-001 **P1–P6**. Every design note below follows from them. If you find yourself
reaching for an SDK feature that has no Gaia equivalent (sessions, resume, skills, subagents,
SDK hooks for policy, SDK cost), stop: that feature is out of bounds.

The native runtime is **10b**, a parallel package; do not build it. Wiring runtimes into the
graph is **12b**.

## 1. Read first, in this order
1. `docs/decisions/ADR-001-agent-runtimes.md`: whole, especially **Portability** and
   **Consequences** 1–6.
2. `docs/implementation/10-agent-runtimes.md`: "Shared requirements" 1–11, "Claude Agent SDK
   runtime (10c)", "Tests", and the 10a deviations.
3. `docs/implementation/09-sandboxed-tools-and-command-policy.md`: the 09b section.
4. `docs/implementation/00-README.md`, "Rules for every work package".
5. `docs/backend/README.md`, `runtimes.md`, `gateway.md`, `security.md`, `events.md`.
6. `backend/spikes/runtime_spike/sdk_candidate.py` and its README: the spike that proved this SDK
   works through the gateway under `srt`. **Your design differs from it:** no Bash tool, Gaia's
   tools over in-process MCP instead.
7. The SDK source, the only reliable reference (pinned 0.2.175):
   `backend/.venv/lib/python3.12/site-packages/claude_agent_sdk/`
   - `__init__.py`: `tool`, `create_sdk_mcp_server`, `SdkMcpTool`;
   - `types.py`: `ClaudeAgentOptions` (read **every** field's docstring), `AssistantMessage`,
     `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ResultMessage`, `SystemMessage`,
     `HookMatcher`;
   - `client.py`: `ClaudeSDKClient.connect/query/receive_response/interrupt/disconnect`;
   - `_internal/transport/subprocess_cli.py`: how the CLI is found (`cli_path`, `_bundled/`),
     how `env` is **merged onto `os.environ`**, the `-v` version check, and `open_process`.
8. Gaia code: `gaia/runtimes/{base,support,approvals,orphans,fake,finish}.py`,
   `gaia/tools/registry.py`, `gaia/security/sandbox.py`, `gaia/llm/{base,run_tokens}.py`,
   `gaia/services/costs.py`, `tests/runtime_contract.py`, `tests/test_runtime_contract.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 Module layout (P1)
- `gaia/runtimes/claude_agent_sdk.py` is **the only file in `gaia/` that imports
  `claude_agent_sdk`**. 10b adds `tests/test_vendor_boundaries.py`, which enforces this.
- Keep SDK types inside this module. Its public surface is `ClaudeAgentSdkRuntime` and
  `build_wrapper_script(...)`, plus plain Gaia types.
- `gaia/runtimes/finish.py` (`FinishArgs`) is already on your branch, written by the lead. Both
  runtimes share it. Do not edit it.

### 2.2 Tools: Gaia's registry over in-process MCP (P3)
- **Turn every built-in tool off: `tools=[]`.** Read the docstring: an empty list disables all
  built-in tools. Also set `disallowed_tools` to the known built-ins as a second layer: `Bash`,
  `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Glob`, `Grep`, `LS`, `WebFetch`,
  `WebSearch`, `Task`, `TodoWrite`, `Skill`, `KillShell`, `BashOutput`.
- Serve the task's `ToolRegistry`, the subset the role may use, as **one in-process server**
  named `gaia`:
  - `create_sdk_mcp_server(name="gaia", version="1", tools=[...])`, with one `SdkMcpTool` per
    registry spec;
  - build each with the `tool(name, description, input_schema)` decorator, passing the spec's
    **JSON Schema dict** as `input_schema`; check that the decorator accepts a JSON-Schema dict.
    The handler calls `await registry.call(name, args, ctx, call_id=<id>)` and returns
    `{"content": [{"type": "text", "text": result.content}], "is_error": not result.ok}`;
  - the handler does not receive the model's tool-use id. Use a uuid7 per call as `call_id`, and
    note it as a deviation from 10b, where it is the model's id;
  - set `strict_mcp_config=True`, so no repo `.mcp.json` or user server loads.
- Add one more in-process tool, `finish`, with `FinishArgs`'s JSON schema. Its handler stores the
  validated args on the runtime's per-task state and returns "finished". The run ends on the next
  `ResultMessage`. Invalid args get `is_error: true` with the validation message. **Do not use
  `output_format`** for this. A `finish` tool behaves identically in both runtimes, and the same
  prompt works for both (P5).
- **Permissions:** `permission_mode="dontAsk"`, plus `allowed_tools=[f"mcp__gaia__{n}" for n in
  names]` with the **exact names**, no wildcard. Anything else is denied without prompting.
  Policy and approvals happen **inside** `registry.call` (09b), exactly as in the native runtime.
- **Belt and braces:** add a `PreToolUse` hook (`HookMatcher(matcher=None, hooks=[...])`) that
  denies any tool whose name does not start with `mcp__gaia__`, with the reason "only Gaia tools
  are available". It must never be reached. Test it directly by calling the hook function.

### 2.3 No repo or user config, no SDK state (P4)
- `setting_sources=[]`, so no `.claude/`, `CLAUDE.md` or user settings load.
- **Never** set `resume`, `continue_conversation`, `fork_session` or `enable_file_checkpointing`.
  Never read `total_cost_usd` or `model_usage` cost fields for anything but a debug log. Add a
  test that greps your module's source for these names and fails if any appears outside a comment
  explaining the ban.
- `system_prompt` is the same `gaia/runtimes/prompts/junior_system.md` text 10b uses, **or**, for
  `task.role == lead`, `gaia/runtimes/prompts/lead_system.md`, which you create. It is the same
  structure, plus: you are the senior engineer; you may take on a whole failed step; still one
  step. Pass the prompt as a plain string, **never** the `claude_code` preset (P3: no
  vendor-specific prompt). If 10b's `junior_system.md` does not exist yet on your branch, create
  `junior_system.md` with the content rules from doc 10's native section. The lead reconciles the
  two at merge.
- `max_turns=task.max_turns`, `model=<configured model>` (constructor argument, default
  `claude-opus-5-5`), `cwd=str(task.worktree.work_tree)`.
- Set `extra_args` only if needed. Do **not** pass `--dangerously-skip-permissions` in any form.

### 2.4 Environment and sandbox: the `cli_path` wrapper (requirements 2 and 11)
The SDK's `env` is **merged onto Gaia's own `os.environ`** (read `subprocess_cli.py`). Gaia's
process may hold real provider keys in memory and environment. So the CLI is never launched
directly. `cli_path` points at a **per-task wrapper script** that rebuilds the environment from
scratch and enters the sandbox:
- `build_wrapper_script(*, srt_path, settings_path, cli_binary, pid_file) -> str` is a pure
  function that returns the script text. The runtime writes it to `<task_dir>/claude-wrapper`
  with mode `0o700`. Its shape:
  ```sh
  #!/bin/sh
  # Written by Gaia (doc 10c). Rebuilds the CLI's environment from scratch and enters srt.
  printf '%s\n' "$$" > '<pid_file>'
  exec /usr/bin/env -i \
    PATH='<agent path>' HOME='<task_dir>' TMPDIR='<task_dir>' LANG=C.UTF-8 \
    ANTHROPIC_BASE_URL="$GAIA_ANTHROPIC_BASE_URL" ANTHROPIC_API_KEY="$GAIA_RUN_TOKEN" \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 \
    MCP_TOOL_TIMEOUT='<ms>' \
    CLAUDE_CODE_ENTRYPOINT="$CLAUDE_CODE_ENTRYPOINT" CLAUDE_AGENT_SDK_VERSION="$CLAUDE_AGENT_SDK_VERSION" \
    '<srt_path>' --settings '<settings_path>' -- /usr/bin/env -u NO_PROXY -u no_proxy '<cli_binary>' "$@"
  ```
  - Every interpolated path is quoted with `shlex.quote`, and the script refuses (`ValueError`) a
    path containing a newline or NUL.
  - The token is **not** in the script. It arrives as `GAIA_RUN_TOKEN` through the SDK's `env`
    option. That is the only place it appears, and the wrapper renames it.
  - `ANTHROPIC_BASE_URL` arrives the same way, as `GAIA_ANTHROPIC_BASE_URL`.
- `MCP_TOOL_TIMEOUT` is `int((approval_timeout_s + 120) * 1000)`, so an in-turn approval wait does
  not trip the CLI's tool timeout. Whether this bundled CLI honours that variable is **not
  verifiable offline**. Write it, and list it under "Open questions" for the lead's live check.
- `cli_binary` is the SDK's bundled CLI: `Path(claude_agent_sdk.__file__).parent / "_bundled" /
  "claude"`. Fail clearly if it is missing.
- The srt settings come from `SandboxSpec(worktree=<worktree>, tmp_dir=<task_dir>,
  data_dir=settings.data_dir, toolchain_read=settings.sandbox_toolchain_read,
  gateway=f"{settings.host}:{settings.port}", allowed_domains=())`. With `tools=[]` the CLI never
  touches the worktree itself; Gaia's in-process tools do. Check what form `gateway` takes in
  `sandbox.py` and the spike README (an IP-literal host:port).
- The SDK also runs `cli_path -v` for a version check. The wrapper handles that like any other
  invocation. It writes the pid file too, which is harmless; it is overwritten by the real launch.

### 2.5 The run token and the gateway (requirement 2; P4: cost truth is the gateway)
- Mint a per-task run token (`run_tokens.mint(..., provider="anthropic", ttl_s=7200)`) in its own
  transaction, and revoke it in `finally`.
- The base URL is `http://<host>:<port>/llm/anthropic`, the gateway route 07b serves Claude on.
- **Usage for `AgentResult.usage`:** build it from the `ResultMessage.usage` **token counts**,
  priced with Gaia's `PriceTable` for the configured model. Never use `total_cost_usd`. The
  gateway's `llm_usage` rows remain the authority for budgets.

### 2.6 The run loop (`run_task`)
1. Prepare a per-task dir (`tempfile.mkdtemp(prefix="gaia-sdk-")`, mode 0700), the token, the srt
   settings, the wrapper and the `ToolContext` (via an injected `context_factory`, the same
   signature as 10b's).
2. `async with ClaudeSDKClient(options) as client:`, then `await client.query(task.instructions)`,
   then `async for message in client.receive_response():`, mapping each message:
   - an `AssistantMessage` `TextBlock` → `EventType.AGENT_MESSAGE` `{content}`;
   - a `ThinkingBlock` → nothing; it is not shown, and requirement 10 keeps the model's history
     untouched anyway;
   - a `ToolUseBlock` → nothing. The registry emits `agent.tool_call` and `agent.tool_result`
     itself, so emitting here would duplicate them;
   - a `ResultMessage` → `EventType.AGENT_USAGE` with token counts, then stop iterating.
3. Status mapping:
   - `finish` called and `ResultMessage.is_error` false → `completed`;
   - `ResultMessage.subtype` is the max-turns one (read `types.py` for the exact value) →
     `failed`, "max turns reached";
   - `is_error` true or no `finish` → **check the budget**. An injected `budget_exhausted:
     Callable[[AgentTask], Awaitable[bool]]` (default: load the run and call
     `costs.remaining_budget(run) <= 0`) returns `budget_exceeded` when true, otherwise `failed`,
     with a short redacted error from `ResultMessage.errors`/`result`. Don't parse error strings
     for "budget": the gateway's 403 reaches the CLI as a generic API error.
4. **Cancellation:** `cancel(task_id)` calls `client.interrupt()` on the live client (keep a dict
   by task id), waits up to 5 s, then cancels the asyncio task. `finally` always disconnects,
   revokes the token, removes the pid entry and deletes the task dir.
5. **Orphans (requirement 9):** after `connect`, read the pid file (retry for up to 2 s) and add
   it to the `OrphanRegistry` with `argv0="srt"`, or whatever the process's `comm` will be; check
   what `is_ours` compares. Remove it in `finally`. The SDK does not start a new process group,
   so record that orphan cleanup kills the wrapper's pid, and that srt's own child handling must
   be verified live.

### 2.7 Testability (you cannot run the real CLI: no network, and macOS cannot nest sandboxes)
- Inject the client class: the constructor takes `client_factory: Callable[[ClaudeAgentOptions],
  ClientLike] = ClaudeSDKClient`. `ClientLike` is a small `Protocol` in your module (`__aenter__`,
  `__aexit__`, `query`, `receive_response`, `interrupt`).
- Tests use a `FakeSdkClient` (in `tests/`). It records the options and yields scripted SDK
  message objects; build real `AssistantMessage`, `TextBlock` and `ResultMessage` instances, since
  the SDK is installed. A scripted step can **invoke an in-process tool**: the fake looks up
  `options.mcp_servers["gaia"]`, finds the tool by name, and awaits its handler. Read how
  `create_sdk_mcp_server` exposes its tools; if that is awkward, keep your own `name → SdkMcpTool`
  map on the runtime and let the fake use it. Either way, the real handler calls the real 09b
  registry.
- **Options test:**
  - `tools == []`, `setting_sources == []`, `strict_mcp_config is True` and
    `permission_mode == "dontAsk"`;
  - `allowed_tools` is exactly the `mcp__gaia__*` list;
  - `cli_path` is the wrapper, and `env` holds only `GAIA_RUN_TOKEN` and
    `GAIA_ANTHROPIC_BASE_URL`, no `ANTHROPIC_API_KEY`;
  - none of the banned options is set.
- **Wrapper test:**
  - the generated script **runs** under `/bin/sh` with a stub "srt" (a tiny script in
    `tmp_path` that prints its argv and env as JSON) and a stub CLI, using `run_proc`;
  - the stub sees `NO_PROXY` unset; `ANTHROPIC_API_KEY` equals the planted token; `PATH` and
    `HOME` match; and **no** planted parent variable such as `ANTHROPIC_API_KEY_REAL` or
    `DEEPSEEK_API_KEY` survives, even though the test sets them in the wrapper's environment;
  - the pid file is written;
  - quoting holds for a path with a space and a quote.
- **Contract suite:** add `ClaudeAgentSdkRuntime` with `FakeSdkClient` to
  `tests/runtime_contract.py`'s parametrisation. Pass every test, marking in-process-only N/A
  items the way 10b does.
- **Live test** (`tests/test_claude_sdk_live.py`, skipped unless `GAIA_LIVE_CLAUDE_SDK=1` and
  `GAIA_SRT_PATH` are set; the lead runs it): a real run on a toy worktree through a running
  gateway, asserting a file was written through `mcp__gaia__write_file`.

### 2.8 `gaia dev run-task --runtime claude_sdk`
10b extends `dev run-task` with `--runtime {fake,native}`. To avoid a conflict, **you** add only a
function `async def _run_claude_sdk_task(args, settings) -> int` to `gaia/cli.py` and the
`claude_sdk` choice. The lead merges the two at merge time. It composes the same pieces as 10b:
the default registry subset (`ALL_TOOLS` for the lead role), `SrtCommandRunner`,
`DbApprovalBroker` and `DbAuditSink`.

## 3. Files
**You may create:**
- `gaia/runtimes/claude_agent_sdk.py`, `gaia/runtimes/prompts/lead_system.md`, and
  `gaia/runtimes/prompts/junior_system.md` if it is absent;
- `tests/test_claude_sdk_runtime.py`, `tests/test_claude_sdk_wrapper.py`,
  `tests/test_claude_sdk_live.py`, `tests/sdk_fakes.py`.

**You may edit:**
- `tests/runtime_contract.py`, `tests/test_runtime_contract.py` (add your runtime);
- `gaia/cli.py` (2.8 only);
- `docs/implementation/10-agent-runtimes.md`: add "Decisions and deviations (as built) — 10c".

**You must not touch:**
- `pyproject.toml`, `uv.lock` and every `[tool.*]` section (the lead added the dependency);
- `gaia/runtimes/finish.py`, `gaia/tools/`, `gaia/security/`, `gaia/llm/`, `gaia/orchestrator/`,
  `gaia/api/`;
- `.github/`, migrations, other docs.

## 4. Non-negotiable rules
- No provider key reaches the CLI process, ever. Prove it with the wrapper test.
- `claude_agent_sdk` is imported only in `gaia/runtimes/claude_agent_sdk.py` (P1).
- No built-in tool, no repo config, no SDK session state, no SDK cost (P3, P4).
- No `shell=True`; no `subprocess` outside tests. The SDK spawns its CLI itself; that is the
  accepted exception, recorded in ADR-001. Your own code spawns only through `run_proc`.
- Check every SDK API against the installed 0.2.175 source; the spike's code is evidence, not
  reference.
- mypy strict. If the SDK's types force a `cast` or an `Any`, comment why. No new `# type: ignore`
  without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: the wrapper script and its test → options construction and its test → the tool bridge
   (registry → `SdkMcpTool`) → `FakeSdkClient` → `run_task` with status mapping → cancel and
   orphans → contract suite → CLI → live test file.
3. Checks, all of which must pass (run them from `backend/`):
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run mypy`
   - `uv run pytest -q` (100% coverage)

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
- **Summary**
- **Files changed**
- **Deviations**, each with a reason
- **External APIs used**: each SDK symbol, with the installed file and line you checked
- **Security-relevant changes**
- **Portability (ADR-001 P1–P6)**: for each rule, what this package does
- **Tests added**
- **Open questions**: things only a live run can confirm
- **Final check output**
