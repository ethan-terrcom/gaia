# Handoff: 11 — Lead planning & review

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs.
The lead does the commit and merge, and the report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **11 — Lead planning & review**: the code that turns a ticket
into a `Plan` and a diff into a `Review`, through single structured-output calls to Claude. The
**prompts are already written by the lead**, in `backend/gaia/orchestrator/prompts/lead_plan.md`
and `lead_review.md`. Load them; **do not edit them**. The lead engineer reviews your work, then
commits and merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/11-lead-planning-and-review.md`: your contract.
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §3–§4.
4. `docs/backend/README.md`, then `docs/backend/llm.md`, `security.md`, `gateway.md` and
   `python-concepts.md`.
5. `docs/reviews/2026-11-13-review-07a.md`: what the Anthropic client gets right, and the
   API-shape bug it had.
6. The code you build on:
   - `gaia/llm/{base,anthropic_client,fake,untrusted}.py`;
   - `gaia/services/costs.py`;
   - `gaia/security/{commands,paths}.py`;
   - `gaia/db/{models,repo}.py`, `gaia/domain/{enums,state}.py`;
   - `gaia/cli.py`;
   - the two prompt files;
   - `tests/test_anthropic_client.py`, `tests/test_fake.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**Structured outputs in the client (`gaia/llm/anthropic_client.py`, `gaia/llm/fake.py`, `gaia/llm/base.py`)**
- Add `async def parse(self, *, system: str, messages: Sequence[LLMMessage], output_model:
  type[T], max_tokens: int = 16000, enable_fallback: bool = False) -> ParsedResponse[T]` to
  `AnthropicClient`. `T` is bound to `pydantic.BaseModel`.
- `ParsedResponse[T]` is a frozen generic dataclass in `base.py`: `output: T`,
  `usage: LLMUsage`, `model: str`, `request_id: str | None`, `stop_reason: str`.
- Use the SDK's own parse helpers, in **anthropic 2.8.0** as installed. Read
  `backend/.venv/lib/python3.12/site-packages/anthropic/resources/messages/messages.py` (`parse`)
  and `.../resources/beta/messages/messages.py` (`parse`, which accepts `fallbacks` and `betas`):
  - without fallback: `client.messages.parse(model=…, max_tokens=…, system=…, messages=…,
    output_format=output_model, output_config={"effort": "high"}, cache_control={"type": "ephemeral"})`;
  - with fallback: `client.beta.messages.parse(…, betas=["server-side-fallback-2026-07-01"],
    fallbacks="default")`, the same pattern 07a uses for `complete`.
  - **Check `_validate_output_config_conflict`** in the installed source. Passing
    `output_config={"effort": "high"}` together with `output_format=` must be legal. If the SDK
    refuses the combination, put `effort` where the source says it goes, and record the deviation.
    Do not guess.
  - Do **not** send `tools` or `tool_choice` with a parse call. Do not send `thinking` (07a's
    rules).
  - Stream when `max_tokens > STREAM_MAX_TOKENS_THRESHOLD`, if the SDK supports it for parse. If
    it does not, keep `max_tokens ≤ 16000` for parse, and say so.
- Handle `stop_reason` exactly as `complete` does: record usage first, then raise `LLMStop` for
  `refusal`, `max_tokens`, `pause_turn` and `model_context_window_exceeded`. A `parsed_output` that
  is `None` with stop `end_turn` raises `LLMStop("invalid_output")`.
- `FakeLLM` gets `parse` too: scripted `ParsedResponse`s, or a scripted `LLMStop` to raise,
  recording usage like `complete`. Add an `LLMProtocol`-style `StructuredLLM` Protocol in `base.py`
  with `parse`, so `lead.py` depends on the protocol, not the client class.

**Output models (`gaia/domain/plans.py`):** exactly as in the doc, with these validators:
- `Plan.steps` has 1–10 items;
- `PlanStep.title` is at most 200 characters, and `instructions` 1–20,000;
- `test_command` is `None` or 1–64 items, each 1–1000 characters, with no NUL.

Keep the models plain: structured outputs needs a JSON schema the API accepts. **No** custom
`Field` regex, discriminated unions or recursive types.

**`gaia/orchestrator/lead.py`**
- `class LeadEngineer` has `__init__(self, llm: StructuredLLM, policy: CommandPolicy, *, repo_context_budget_tokens: int = 100_000)`.
- `async plan_ticket(self, ticket: TicketInput, repo_context: RepoContext, *, extra_allow=()) -> Plan`.
  - `TicketInput` is a frozen model: `id`, `title`, `body`.
  - The user message is built from `wrap_untrusted(...)` blocks: the ticket, then the repo context.
    The system prompt is the file text, loaded **once** at import and never formatted (task 3).
  - After parsing, validate every `step.test_command` with
    `policy.evaluate(argv, extra_allow=extra_allow)`. Any `deny` triggers **one** retry, with the
    errors appended as a new user message. If it fails again, raise `PlanRejected(reasons)`.
  - `ask` is fine: a human approves it at run time.
- `async review_step(self, plan, step_index, diff: str, test_output: str | None) -> Review` and
  `async final_review(self, plan, full_diff: str) -> Review`.
  - These use `lead_review.md` as the system prompt.
  - Diffs are truncated **per file**: split on `diff --git` headers, cap each file at 20,000
    characters and the total at 150,000. Append a plain note listing truncated files, for example
    `[truncated: a.py (40 KB → 20 KB), 3 files omitted]`. The test output keeps its last 20,000
    characters.
  - The truncation is a pure function, `truncate_diff(diff, *, per_file, total) -> tuple[str, list[str]]`,
    tested on its own.
- **Refusals (task 5):** an `LLMStop` with `stop_reason="refusal"` after any fallback raises
  `LeadEscalation(category=…, request_id=…)`. Never retry a refusal. `plan_ticket` and the reviews
  call the LLM with `enable_fallback=True`.
- **Usage (task 4):** the client records it. `LeadEngineer` just uses a client constructed with a
  `DbUsageRecorder(..., agent_id=<lead agent id>)`.

**Repo context (`gaia/orchestrator/repo_context.py`)**
- `async build_repo_context(root: Path, ticket: TicketInput, *, budget_chars: int = 400_000) -> RepoContext`.
  - `root` is the repository's checkout. Read it only; never write.
  - Include `README*` at the root (first match, capped at 40,000 characters).
  - Include a depth-2 tree from `os.scandir`. Skip the hidden directories `.git`, `.venv`,
    `node_modules`, `__pycache__`, `dist` and `build`, and cap the tree at 2,000 entries.
  - Include the contents of files whose **repo-relative paths appear in the ticket text**. Find
    them with a regex for path-like tokens, then check each candidate with
    `security.paths.confine(root, candidate)`. A denied candidate is silently skipped, and so is
    anything that isn't a regular file. Cap each file at 40,000 characters.
  - Stop adding when `budget_chars` is reached, and record what was dropped.
- `RepoContext` holds `readme: str | None`, `tree: str`, `files: tuple[tuple[str, str], ...]` and
  `omitted: tuple[str, ...]`. It has a `render()` method that produces the text for `wrap_untrusted`.

**CLI: `gaia dev plan <ticket_id>`** (the acceptance criterion)
- It loads the ticket and its repository from the DB of `GAIA_DATA_DIR` (workspace from the seeded
  principal) and builds the repo context from `repository.local_path`.
- It creates a planning-only `run` row (`budget_usd = settings.run_budget_usd`), then calls
  `plan_ticket` with an `AnthropicClient` whose key comes from the keychain
  (`SecretName.ANTHROPIC_API_KEY`) and whose recorder is a `DbUsageRecorder` for that run and the
  seeded `lead` agent.
- It prints the plan as indented JSON plus one line: `cost: $X.XXXX (N input / M output tokens)`.
- Finally it transitions the run to `cancelled`, so 12's startup recovery never resumes it. Use
  `transition()`.
- Make the client factory injectable (`_make_client` module function), so tests pass a `FakeLLM`.
  The lead runs the real call.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/llm/{base,anthropic_client,fake}.py` (`parse` and its types only; do not change
  `complete`'s behaviour);
- `backend/gaia/domain/plans.py`;
- `backend/gaia/orchestrator/{lead,repo_context}.py` and `backend/gaia/orchestrator/__init__.py`;
- `backend/gaia/cli.py` (`dev plan` only; `dev` subcommands may already exist, so extend them);
- `backend/tests/**`;
- a "Decisions and deviations (as built)" section at the end of
  `docs/implementation/11-lead-planning-and-review.md`.

**You must not:**
- edit `backend/gaia/orchestrator/prompts/*.md` (lead-owned; if you think a prompt is wrong, say so
  in the report);
- edit other docs, `.github/`, `[tool.*]` in `pyproject.toml` or `uv.lock`;
- add dependencies or migrations;
- build the graph (12) or the runtimes (10).

## 4. Non-negotiable rules
- **Prompts are byte-stable:** a golden test asserts that the system prompt equals the file
  content. There must be no timestamps, ids or ticket text in it; volatile content goes in the
  user message.
- **Untrusted content** (ticket, README, files, diffs, test output) only ever reaches the model
  inside `wrap_untrusted(...)`.
- No real network calls in tests. No secrets anywhere.
- **Library APIs:** confirm every SDK parameter against the installed `anthropic` 2.8.0 source and
  name the file in your report.

## 5. How to work
1. Plan first: the files, the public API with signatures, and how each Test in the doc is proven.
2. Build in this order: `ParsedResponse` and `parse` (client plus fake, with tests using a stubbed
   SDK client as 07a's tests do), then `plans.py`, then `truncate_diff`, then `repo_context`, then
   `lead.py`, then the CLI.
3. Required tests:
   - a valid plan parses;
   - an invalid `test_command` triggers exactly one retry, then succeeds (and another version where
     it fails, raising `PlanRejected`);
   - a `revise` verdict round-trips;
   - a refusal escalates without retrying;
   - a golden test of system-prompt stability;
   - every untrusted part is wrapped;
   - diff truncation per file and in total;
   - `repo_context` ignores `../` and `.git` paths named in a ticket;
   - the CLI with `FakeLLM` creates a run, records usage and ends with the run `cancelled`.
4. Before you finish, all of these must pass, run from `backend/`: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q` (100% coverage of
   `gaia/`).
5. Write the "Decisions and deviations (as built)" section.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Tests in the doc   (each, with the test name that proves it)
## SDK calls   (exact parameters sent for parse with and without fallback; source file checked)
## Deviations from the doc
## Prompt feedback   (anything in lead_plan.md / lead_review.md you think is wrong or ambiguous)
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
