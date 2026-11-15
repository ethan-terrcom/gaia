# Handoff: 18b — Observability: redacted log files, stdlib logs, metrics summary

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is package **18b**, observability. It makes Gaia safe to leave running and possible to debug
afterwards:
- redacted JSON log files with rotation;
- **every** log line redacted, including the standard library's and uvicorn's (open review
  findings **F8** and **E4**);
- a metrics summary endpoint.

The `GAIA_FAKE_LLM=1` demo mode in doc 18 depends on the orchestrator wiring (12b), which is being
built now, so it is **not** in this package. Leave `# TODO(gaia-18b-2)` where it would go.

## 1. Read first, in this order
1. `docs/implementation/18-budgets-observability-hardening.md`, the 18b section.
2. `docs/reviews/2026-11-13-code-review-01-05.md`, finding **F8**, and
   `docs/reviews/2026-11-14-review-13.md`, finding **E4** (`?ticket=` in uvicorn's access log).
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/backend/config-and-secrets.md` (logging and redaction), `api.md`, `database.md`.
5. Code:
   - `gaia/config/logging.py`, `gaia/security/redaction.py`, `gaia/config/settings.py`;
   - `gaia/cli.py` (`_serve`), `gaia/api/app.py` (the lifespan and router includes),
     `gaia/api/routes/costs.py` (a recent read-only route; follow its pattern);
   - `gaia/db/{models,repo}.py` (`Run`, `LlmUsage`, `Approval`);
   - `tests/test_logging*.py` and `tests/test_redaction*.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 Every log line is redacted (F8, E4)
- In `configure_logging`, route the **standard library's** `logging` through structlog with
  `structlog.stdlib.ProcessorFormatter`, attached to a handler on the **root** logger. Its
  `foreign_pre_chain` must include `redaction_processor`, so a `logging.getLogger("httpx").info(…)`
  line or a uvicorn line is redacted like a structlog one. Read the installed structlog's
  `stdlib.py`; `ProcessorFormatter` takes `processors=[…, ProcessorFormatter.remove_processors_meta,
  renderer]` and `foreign_pre_chain=[…]`. Check its exact signature there.
- **E4:** uvicorn's access log line contains the request path **and query string**
  (`GET /api/v1/stream?ticket=…`). Install a `logging.Filter` on the `uvicorn.access` logger that
  rewrites the record so the query string is dropped. Read how uvicorn formats access records in
  `backend/.venv/lib/python3.12/site-packages/uvicorn/logging.py` (`AccessFormatter`; `record.args`
  is a tuple whose third item is the full path). The result is `GET /api/v1/stream 200`. Unit-test
  it with a real `LogRecord` built the way uvicorn builds one.
- `_serve` must stop uvicorn from installing its own log config over ours: pass
  `log_config=None` to `uvicorn.run` and call `configure_logging(...)` first. Check the
  `uvicorn.run` signature in the installed source. Uvicorn's loggers then propagate to the root
  handler.
- Make sure each line appears **once**: no duplicates from both a uvicorn handler and the root.
  Test it.

### 2.2 JSON log files with rotation
- Add settings: `log_dir: Path` (default `{data_dir}/logs`, derived like `checkpoint_path`; read
  how `settings.py` derives paths from `data_dir`), `log_file_max_bytes: int = 10_000_000` and
  `log_file_backups: int = 5`.
- `configure_logging(*, level, stream=None, log_dir: Path | None = None, max_bytes=…, backups=…)`.
  When `log_dir` is given, also attach a `logging.handlers.RotatingFileHandler` at
  `log_dir/gaia.jsonl` (mode 0600; the directory 0700, created if missing), formatted with the
  same `ProcessorFormatter` but `structlog.processors.JSONRenderer()`. One JSON object per line,
  redacted, with an ISO UTC `timestamp`, `level`, `event` and the bound fields.
- The console keeps the existing human-readable renderer.
- Structlog's own loggers must reach **both** handlers, so the console and the file see the same
  events. The standard way: switch structlog to `structlog.stdlib.LoggerFactory()` with
  `structlog.stdlib.ProcessorFormatter.wrap_for_formatter` as the last processor; the formatter
  then does the rendering. Keep `redaction_processor` in structlog's own chain **and** in
  `foreign_pre_chain`. Keep `cache_logger_on_first_use=False` and the lazy stderr behaviour
  (`_CurrentStderr`, review 11's L1). Tests capture output.
- `gaia serve` passes `log_dir=settings.log_dir`. CLI commands other than `serve` keep the console
  only.
- Tests:
  - a structlog line and a stdlib line both appear in the file as JSON, each once;
  - a planted secret (`register_secret("sk-ant-planted-…")`, plus a `gaia_rt_…` token pattern) is
    masked in the file **and** on the console;
  - rotation at a tiny `max_bytes` produces `.1`;
  - the file and directory modes.

### 2.3 `GET /api/v1/metrics/summary`
A read-only, workspace-scoped route in `gaia/api/routes/metrics.py`. SQL goes in `gaia/db/repo.py`
(a `MetricsRepo`, or functions beside `daily_llm_usage_by_provider`), never in the route. It
returns `MetricsSummary`:
- `runs_by_status: dict[str, int]`;
- `success_rate: float | None` = completed / (completed + failed + cancelled), or `None` when the
  denominator is 0;
- `avg_cost_per_run_usd: Decimal | None`, over finished runs;
- `cost_by_model: list[{provider, model, cost_usd}]` and `cost_by_day: list[{day: date, cost_usd}]`,
  for the last `days` (query parameter, 1–90, default 14, UTC days, from `llm_usage.created_at`);
- `approval_wait_s: {count, avg, p50, p90} | None`, over decided approvals (`decided_at -
  created_at`) in the window. Compute the percentiles in Python over the fetched durations, which
  is fine at this scale; say so in the docstring.

SQLite returns `SUM` as a float, so pass every money value through `str` to `Decimal`, as
`daily_llm_usage_cost_usd` does. Test each field, with rows outside the window excluded, another
workspace's rows excluded, and the empty database (all zeros or `None`, never a 500). Regenerate
`frontend/src/api/openapi.json` with `uv run python scripts/export_openapi.py`, and the types with
`npm run gen:api` (`cwd: "frontend"`).

### 2.4 OpenTelemetry
**Not** in this package; doc 18 says optional and off by default. Leave `# TODO(gaia-18b-otel)` in
`logging.py`.

## 3. Files
**You may create:**
- `gaia/api/routes/metrics.py`;
- tests: `tests/test_logging_files.py`, `tests/test_logging_stdlib.py`, `tests/test_metrics_api.py`.

**You may edit:**
- `gaia/config/logging.py`, `gaia/config/settings.py` (the `log_*` settings);
- `gaia/cli.py` (`_serve` only), `gaia/api/app.py` (the router include only);
- `gaia/db/repo.py` (metrics queries), `gaia/domain/schemas.py` (`MetricsSummary` and its parts);
- existing logging tests;
- `frontend/src/api/openapi.json` and `schema.d.ts` (regenerated only);
- `docs/implementation/18-budgets-observability-hardening.md`: add "Decisions and deviations (as
  built) — 18b".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/orchestrator/`, `gaia/runtimes/`, `gaia/tools/`, `gaia/llm/`;
- `frontend/src` other than the generated files;
- other docs.

Four other juniors are building 12b-1, 12b-2, 17e and 17f in parallel. 12b-1 also edits
`settings.py`, so add your settings in **one block**, with a `# --- logging (18b) ---` comment.

## 4. Non-negotiable rules
- No secret ever reaches a log file or the console unredacted. That is the point of this package.
  Prove it in tests with planted values.
- The log directory and files are owner-only (0700 and 0600).
- SQL only in `gaia/db/`. The new route requires the token and is workspace-scoped.
- Check structlog's and uvicorn's APIs against the installed source.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: the stdlib bridge and redaction → the E4 filter → file handler and rotation → `_serve`
   → settings → the metrics queries → route → openapi.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
   `uv run pytest -q` (100% coverage). Frontend (`cwd: "frontend"`), after regenerating:
   `npm run lint`, `npm run typecheck`.

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · External APIs used · Security-relevant changes · Tests added ·
Open questions · Final check output.
