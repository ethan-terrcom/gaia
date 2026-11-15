# Handoff: 10c part 2 — finish test coverage (do not redesign)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits; your report goes through `finish`.

You are finishing package **10c** (the Claude Agent SDK runtime). The implementation is done and
was reviewed. The lead changed it after review; read the "Lead changes" list. **Your only job is
to bring `uv run pytest --cov` back to 100% line and branch coverage by adding tests.** Do not
change production code unless a line is genuinely unreachable. In that case, delete the dead
code; never add `pragma: no cover`, and record the deletion in your report.

## Read first
- `docs/implementation/handoffs/10c-claude-agent-sdk-runtime.md`, your original handoff.
- `backend/gaia/runtimes/claude_agent_sdk.py`, the module under test.
- `backend/tests/test_claude_sdk_runtime.py`, `tests/sdk_fakes.py`, `tests/test_claude_sdk_wrapper.py`,
  and `tests/test_cli_dev.py` (how 10a and 10b test the `gaia dev run-task` CLI paths).

## Lead changes since your run (already in the code)
- **C1:** `run_task` now runs the session in a child task (`_run`). `cancel()` cancels that child,
  and `run_task` returns `cancelled`; a cancelled *caller* re-raises.
- **C2:** the wrapper, srt settings and pid file live in a control dir from
  `gaia.tools.shell.sandbox_settings_dir(settings)`, not in `task_dir`.
- **C3:** `MCP_TOOL_TIMEOUT` = approval timeout + `MAX_COMMAND_TIMEOUT_S` (1,800) + 120, in ms.
- The runtime's name defaults to `claude_agent_sdk`, and the dev CLI's run gets
  `budget_usd = settings.run_budget_usd`.

## Missing coverage (from `uv run pytest --cov`)
```
gaia/cli.py                        478-508, 519-572, 579      (_run_claude_sdk_task and its dispatch)
gaia/runtimes/claude_agent_sdk.py  355, 357, 365->367, 378-381, 392->403, 395->394, 400->392,
                                   557, 565-576, 614-615, 629-630, 636-638, 645, 695
```
Open the module, map each line to its branch, and write one focused test per branch:
- `cancel()` with no live client;
- `interrupt()` timing out, and `interrupt()` raising;
- the drain loop with a non-text block and a non-assistant/non-result message;
- `_failed_or_budget_exceeded` both ways;
- the default mint, revoke and budget functions against a real test DB (the suite's database
  fixtures);
- `_bundled_cli` missing, through a monkeypatched path;
- `_short_result_error`'s `stop_reason` fallback;
- the CLI path, with `ClaudeAgentSdkRuntime`'s client factory monkeypatched to the test fake, in
  the style 10b used for `--runtime native`.

Use the existing fakes. Never call the real SDK client or the network.

## Rules
Do not touch `pyproject.toml`, `uv.lock`, other packages or docs. mypy strict. No new
`# type: ignore` or `# noqa` without a reason.

## Checks (from `backend/`)
`uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` ·
`uv run pytest -q --cov` (must say `Required test coverage of 100% reached`).

## Report (pass to `finish`)
Summary · Tests added (which line each covers) · Any dead code deleted, with the reason · Final
check output.
