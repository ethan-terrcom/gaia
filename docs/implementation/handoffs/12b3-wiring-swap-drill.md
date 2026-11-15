# Handoff: 12b-3 — Wire the real orchestrator into the app; the swap drill (ADR-001 P5)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Every
piece of a real run now exists:
- the graph and `RunManager` (12a);
- the execution services (12b-1) and the lead services (12b-2), in `gaia/orchestrator/impl/`;
- both runtimes (10b, 10c), the tools (09b), the gateway (07b) and GitHub (15a/15b).

Your package, **12b-3**, is the **composition root** that builds all of it from settings, and
puts a `RunManager` in the running app. It also delivers ADR-001's **swap drill (P5)**: a test
that runs a whole ticket end to end through the real wiring **with Claude nowhere in it**. That
test is the proof that the owner can migrate away from Claude.

The HTTP routes for runs and approvals are **14**, built in parallel by another junior against the
interface in §2.4. Do not build them.

## 1. Read first, in this order
1. `docs/decisions/ADR-001-agent-runtimes.md`: **Portability P1–P6**.
2. `docs/implementation/12-orchestrator-graph.md`: all sections, including 12a, 12b-1 and 12b-2
   and their lead reviews.
3. `docs/backend/orchestrator.md`, `runtimes.md`, `tools.md`, `gateway.md`, `lead.md`,
   `github.md`, `api.md`.
4. `docs/reviews/2026-11-14-review-{10b,10c,12b1,12b2}.md`.
5. Code:
   - `gaia/orchestrator/{deps,graph,runner,guard}.py`, `gaia/orchestrator/impl/*`;
   - `gaia/runtimes/{native,native_llm,claude_agent_sdk,base,support,approvals,orphans}.py`;
   - `gaia/tools/{registry,shell,fs}.py`, `gaia/security/{audit,sandbox}.py`;
   - `gaia/llm/{providers,anthropic_client,deepseek_client}.py`;
   - `gaia/services/{pull_requests,pr_sync,events,git}.py`;
   - `gaia/api/{app,deps}.py` (the lifespan and `AppServices`), `gaia/config/settings.py`;
   - `gaia/cli.py` (`_run_native_task` and `_run_claude_sdk_task` show how each runtime is
     composed today; your wiring generalises them);
   - tests: `tests/test_orchestrator_real_execution.py` (12b-1's integration test),
     `tests/test_native_llm.py` (the in-process gateway with an ASGI-backed client),
     `tests/test_lead_services_portability.py`, `tests/conftest.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 `gaia/orchestrator/wiring.py`: the one composition root (P2)
```python
@dataclass(frozen=True)
class Wiring:
    deps: GraphDeps
    runtimes: RuntimeRegistry
    approvals: DbApprovalBroker      # the ONE broker instance (see 2.3)

def build_wiring(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    bus: EventBus,
    git: GitService,
    secrets: SecretStore,
    prices: PriceTable,
    # test seams; production passes none of these
    llm_http_client: Any | None = None,          # an httpx client for every LLM client (the swap drill routes it to the in-process gateway)
    command_runner: CommandRunner | None = None, # replaces every SrtCommandRunner (setup, checks, run_command)
    pull_requests: PullRequests | None = None,   # replaces PullRequestService
) -> Wiring: ...
```
- **Runtimes**, registered under the `RuntimeKind` values:
  - `NativeRuntime(name="native_deepseek", llm_factory=gateway_llm_factory(settings=…,
    sessions=…, price_table=prices, model=settings.junior_model, http_client=llm_http_client),
    tools=<default registry>.subset(JUNIOR_TOOLS), context_factory=…)`;
  - `ClaudeAgentSdkRuntime(name="claude_agent_sdk", registry=<default registry>.subset(ALL_TOOLS),
    settings=…, sessions=…, context_factory=…, model=settings.lead_model, price_table=prices,
    orphans=OrphanRegistry(settings.data_dir / "runtime-pids.json"), approval_timeout_s=…)`.

  `<default registry>` is `build_default_registry(search_supports_regex=find_rg(settings) is not
  None)`. Wrap `find_rg` so a bad `rg_path` setting raises at startup with a clear message.
- **The lead's escalation runtime** is whatever `settings.lead_runtime` names. Nothing else in
  wiring picks a vendor; it maps the `RuntimeKind` setting to a registered runtime.
- **Context factories**, one per runtime signature, both building `ToolContext` with:
  - `git`, `CommandPolicy.from_default()`, the **shared broker** (2.3) and
    `DbAuditSink(sessions)`;
  - `events` (the sink the runtime passes in);
  - `commands = command_runner or <lazy SrtCommandRunner>`;
  - `task_dir`: for the native runtime, create
    `{data_dir}/runs/<run_id>/tasks/<task_id>` (mode 0700) inside the factory; 10c passes its
    own;
  - `actor=f"agent:{task.role.value}"`, and `rg_path=find_rg(settings)`.
- **The lazy srt runner:** `class LazySrtRunner` implements `CommandRunner`. Its first `run` calls
  `find_srt(settings)` and builds `SrtCommandRunner(settings, srt_path=…,
  allowed_domains=…)`. **The app must start without srt installed** (local-first, and the UI
  still works); a run that reaches a command then fails with `SandboxUnavailable`'s clear "run
  `make sandbox-install`" message. Use three instances:
  - setup (`allowed_domains=settings.setup_allowed_domains`);
  - checks (`()`);
  - tools (`()`).
- **GraphDeps:**
  - `worktrees=WorktreeSetup(…, commands=<setup runner>)`;
  - `planner=GatewayPlanner(…)`;
  - `steps=RuntimeStepRunner(…, runtimes=registry)`;
  - `checks=SandboxCheckRunner(…, commands=<check runner>, policy=CommandPolicy.from_default())`;
  - `reviewer=GatewayReviewer(…)`;
  - `committer=WorktreeCommitter(…)`;
  - `pull_requests = pull_requests or PullRequestService(sessions=…, git=…, secrets=…, bus=…, …)`;
    read its constructor; 15b added `settings`, `sleep` and `monotonic`;
  - `sessions`, `events=bus`, `settings`.
- **`llm_http_client` plumbing:** `gateway_llm_factory` already accepts `http_client`. The lead's
  `lead_llm` → `build_structured_llm` does not yet:
  - add `http_client: Any | None = None` to `build_structured_llm` and pass it to the adapter;
  - `DeepSeekClient` already takes `http_client`; add the same optional parameter to
    `AnthropicClient` (passed to `anthropic.AsyncAnthropic(..., http_client=...)`; check the
    installed SDK's constructor);
  - add `http_client` to `lead_llm` and to `GatewayPlanner`/`GatewayReviewer` (an optional
    constructor keyword stored and passed through).

  Production passes `None` everywhere.
- **P1 still holds:** `wiring.py` imports adapter modules and Gaia classes only. The
  vendor-boundary test must still pass.

### 2.2 The app lifespan (`gaia/api/app.py`, `gaia/api/deps.py`)
- Add to `AppServices`:
  - `runs: RunManager | None = None`;
  - `approvals: DbApprovalBroker | None = None`;
  - `wiring: Wiring | None = None`.
- In the lifespan, after the services exist and before `yield`:
  1. enter `open_checkpointer(settings)`. Use a `contextlib.AsyncExitStack` held for the lifespan,
     and close it in the existing `finally`;
  2. call `build_wiring(...)` with the production arguments;
  3. `graph = build_graph(wiring.deps, checkpointer=saver)`;
  4. `runs = RunManager(graph, wiring.deps)`, then `await runs.recover()`, which resumes
     interrupted runs (12a);
  5. store `runs`, `approvals` and `wiring` on `AppServices`.
- **Shutdown order** (in `finally`, before `engine.dispose()`): `await runs.aclose()`, then close
  the exit stack (the checkpointer).
- A setting, `orchestrator_enabled: bool = True`. Tests that build the app for unrelated routes
  must not start runs or open a checkpointer file; set it `False` in the shared test settings
  fixture (the same place 15b put `pr_sync_enabled=False`). When disabled, `runs` stays `None`.
- `recover()` failing must not stop the app starting: log `orchestrator.recover_failed` with the
  exception, and continue.

### 2.3 One approval broker (14 depends on this)
In-turn command approvals wait on an in-process waiter **inside a specific `DbApprovalBroker`
instance**. 14's `POST /approvals/{id}/decide` must call `decide` on **that same instance**, or the
waiting agent never wakes. So `build_wiring` creates exactly one `DbApprovalBroker(sessions, bus)`,
used by both context factories, and the lifespan exposes it as `AppServices.approvals`. Test that
both runtimes' contexts hold the identical object.

### 2.4 Interface 14 builds against (do not change it after you report)
- `AppServices.runs: RunManager | None`. `RunManager` keeps `start(ticket_id, *, workspace_id,
  created_by)`, `resume(run_id, decision)`, `cancel(run_id, *, workspace_id)` and `active()`.
- `AppServices.approvals: DbApprovalBroker | None`, with `decide(...)` (10a).
- Do not rename these.

### 2.5 The swap drill: `tests/test_swap_drill.py` (P5)
The most important test in the package. It runs one ticket from `pending` to `completed`
through `build_wiring` with **no Claude anywhere**:
- **Settings:**
  - `planner_provider="deepseek"`, `planner_model="deepseek-v4-pro"`;
  - `junior_runtime=NATIVE_DEEPSEEK`, **and** `lead_runtime=NATIVE_DEEPSEEK` (so even an
    escalation stays off Claude);
  - `orchestrator_enabled=False` (you drive a `RunManager` you build in the test);
  - a temp `data_dir`, with `run_budget_usd` set.
- **The gateway, in-process:**
  - `app = create_app(settings, llm_transport=httpx.MockTransport(fake_deepseek))`, entered with
    its lifespan;
  - an `InMemorySecretStore` holding a fake DeepSeek key, read how the gateway tests provide one;
  - `llm_http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app))`. Check what
    10b's/12b-2's tests used for the OpenAI SDK's http client; the SDKs may need their own
    `httpx2` client type.
- **`fake_deepseek(request)`** answers `POST /chat/completions`, routing by the body:
  - a `response_format` of `json_object` whose system text contains the plan schema → a valid
    one-step **Plan** JSON (test command `["pytest", "-q"]`);
  - `json_object` containing the review schema → **Review** `accept`;
  - with `tools` (the native loop), a scripted sequence:
    1. `write_file` (`hello.py`);
    2. `run_command` (`["pytest","-q"]`);
    3. `finish`.

    It must return the usage fields DeepSeek sends (`prompt_cache_hit_tokens`, …) so the gateway
    meters it. It also **records every request path and model**.
- **`command_runner`:** a fake that returns exit 0 for `pytest`. srt is not available in CI.
- **`pull_requests`:** a fake that returns PR #1, CI `"passed"`, and a no-op merge.
- **Drive it:**
  1. `RunManager(build_graph(wiring.deps, checkpointer=<a temp AsyncSqliteSaver>),
     wiring.deps).start(ticket_id, …)` on a real git repo and ticket (use the test builders);
  2. wait for the `approve_plan` interrupt, then `resume(... approve ...)`;
  3. the same for `approve_pr` and `approve_merge`;
  4. wait for `completed`.

  Follow how 12a/12b-1's integration tests wait.
- **Assert:**
  - the run is `completed`, the ticket is `done`, and the worktree branch has a commit containing
    `hello.py`;
  - **every** request reached the fake as DeepSeek `chat/completions`, with zero Anthropic
    requests;
  - every `llm_usage` row has `provider == "deepseek"`;
  - `run.cost_usd > 0`;
  - no run token is left unrevoked;
  - no `ClaudeAgentSdkRuntime.run_task` was called (patch it to fail the test).

If the native loop's message shapes make the scripted fake awkward, read 10b's
`tests/llm_fakes.py` and the gateway tests for a working DeepSeek response shape.

### 2.6 Also
- A **wiring unit test**:
  - `build_wiring` with default settings registers both runtimes under the enum values;
  - `settings.lead_runtime` and `junior_runtime` pick them;
  - the lazy runners don't call `find_srt` at build time;
  - one broker is shared.
- A **lifespan test**: with `orchestrator_enabled=True` and a temp data dir, the app starts,
  `services.runs` is a `RunManager`, and shutdown closes it. With the flag off, `runs is None`.

## 3. Files
**You may create:**
- `gaia/orchestrator/wiring.py`;
- tests: `tests/test_wiring.py`, `tests/test_swap_drill.py`, `tests/test_app_orchestrator.py`.

**You may edit:**
- `gaia/api/{app,deps}.py` (the lifespan and `AppServices` only);
- `gaia/config/settings.py` (`orchestrator_enabled`);
- `tests/conftest.py` (the flag in the shared settings);
- `gaia/llm/{providers,anthropic_client}.py` (`http_client` only);
- `gaia/orchestrator/impl/{lead_llm,planning,review}.py` (`http_client` pass-through only);
- `docs/implementation/12-orchestrator-graph.md`: add "Decisions and deviations (as built) —
  12b-3".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/api/routes/` (14 is building them in parallel);
- the graph, nodes, runtimes and tools beyond the above;
- other docs.

## 4. Non-negotiable rules
- **P1/P2:** no vendor SDK import in `wiring.py`; runtime and model choices come from settings
  only.
- The app must start with no srt, no provider keys and no network.
- No provider key in any client: LLM clients get run tokens and point at the gateway.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: `http_client` plumbing → `wiring.py` and the unit test → lifespan and settings → the
   swap drill.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy`, `uv run pytest -q` (100% coverage).

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · External APIs used · Security-relevant changes ·
Portability (P1–P6) · Tests added · Open questions · Final check output.
