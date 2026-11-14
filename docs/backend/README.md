# Backend developer guide

> **Audience:** developers working in `backend/` who are comfortable with Python but not
> necessarily with async code, FastAPI, SQLAlchemy 2 or process management. Start with the
> one-page overview in [`backend/README.md`](../../backend/README.md), then read the guide for the
> area you are about to touch.
>
> **Status:** living document, current as of package 05 (2026-11-13).

## How these guides are organised

Each guide follows the same pattern:

1. **What it's for**: the job of that part of the code, in a paragraph.
2. **Files**: what lives where.
3. **How it works**: the main flow, walked through with the real function names.
4. **Why it's built this way**: the decisions that look odd until you know the reason.
5. **Changing it safely**: rules to keep, and the tests that will catch you if you don't.

When a guide uses a technique that intermediate Python developers often haven't met (an
`async with`, a `Protocol`, a FastAPI dependency, a SQLAlchemy flush), it links to a short
explainer in [`python-concepts.md`](python-concepts.md). Read that file once end to end on your first
day. It is the vocabulary for the rest of the guides.

| Guide | Covers | Package docs |
|---|---|---|
| [Config & secrets](config-and-secrets.md) | `Settings`, the keychain `SecretStore`, redaction, logging, the `gaia` CLI | 02 |
| [Database](database.md) | Models, migrations, `ScopedRepo`, status state machines, schemas | 03 |
| [API](api.md) | The app factory, lifespan, middleware, auth, dependencies, errors | 04 |
| [Git & processes](git-and-processes.md) | `run_proc`, `GitService`, `WorktreeService` | 05 |
| [LLM clients & costs](llm.md) | `AnthropicClient`, `DeepSeekClient`, `FakeLLM`, pricing and usage recording | 07a |
| [Python concepts](python-concepts.md) | Explainers for the techniques the code relies on | – |

Guides for the gateway (07b), `runtimes/`, `tools/`, `orchestrator/` and the event stream will be added as
those packages land (07–15).

## Tooling and conventions

These are enforced by tooling, so you'll find out quickly if you break one. Knowing them up front
saves a red CI run.

| Rule | Enforced by | What it means for you |
|---|---|---|
| Only `gaia/services/proc.py` starts processes | ruff `TID251` (banned imports) | Need to run a command? Call `run_proc()`. Importing `subprocess` anywhere else fails lint. |
| No shell, ever | the ban above, plus `run_proc` rejecting a `str` argv | Commands are lists: `["git", "status"]`, never `"git status"`. |
| Strict typing | `mypy --strict` over `gaia` and `tests` | Every function in `gaia/` is fully annotated. `Any` needs a reason in a comment. |
| Warnings are errors in tests | `filterwarnings = ["error"]` | An un-awaited coroutine or a deprecated call fails the test run. |
| Tests never touch the real keychain or `~/.gaia` | `tests/conftest.py` | It installs an in-memory keyring and strips `GAIA_*` env vars. Use `tmp_path` for files. |
| Live calls are opt-in | the `live` pytest marker, deselected by default | Mark anything that needs the network or a real key `@pytest.mark.live`. |
| Exact dependency versions | `uv.lock` (`uv sync --locked` in CI) | Add packages with `uv add`; never hand-edit the lock. |
| No secrets in git | gitleaks in pre-commit and CI | It includes a rule for DeepSeek's key shape. |
| Coverage | `pytest --cov` (currently 100%, not yet enforced) | New code comes with tests, including the failure paths. |

Every work package also follows the rules in
[`docs/implementation/00-README.md`](../implementation/00-README.md#rules-for-every-work-package).

## Day-one commands

```sh
make setup                         # install everything, add pre-commit hooks
make migrate                       # create ~/.gaia/gaia.sqlite
cd backend
uv run pytest -q                   # fast test run
uv run pytest tests/test_git_service.py -k hook   # one area
uv run mypy                        # strict type check
uv run ruff check . && uv run ruff format .
uv run gaia --help                 # the CLI
```

## Where the "why" is recorded

- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) covers system-level decisions: stack, security model,
  workflow.
- `docs/implementation/NN-*.md` gives each package's plan, with a **"Decisions and deviations (as
  built)"** section listing what actually shipped and why. When code surprises you, look there first.
- `docs/reviews/` holds the plan review and code reviews, including open findings.
