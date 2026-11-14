# Python concepts used in the backend

> **Audience:** intermediate Python developers. Each section explains one technique the backend
> relies on: what it is, why Gaia uses it, and where to see it. Read the whole file once, then use
> it as a reference when a guide links here.

## async and await

**What.** An `async def` function is a *coroutine*: calling it doesn't run it, it returns an object
you must `await`. While one coroutine waits (for a database row, a subprocess, a network reply),
the **event loop** runs others on the same thread. That is how one server process handles many
requests at once without threads.

```python
async def current_sha(self, repo: Path) -> str:
    result = await self._run("rev-parse", "--verify", "HEAD^{commit}", cwd=repo)  # waits here
    return result.stdout.strip()
```

**Two rules that bite.**
- **Forgetting `await`** gives you a coroutine object instead of a result. Python only warns, and
  Gaia's tests turn that warning into a failure (`filterwarnings = ["error"]`).
- **Blocking calls freeze everything.** `time.sleep()`, `subprocess.run()` or a slow file operation
  inside `async def` stops *every* request until it finishes. ruff's `ASYNC` rules flag the common
  ones, which is why small blocking helpers such as `_resolved()` in `services/git.py` live in plain
  `def` functions. Blocking calls that matter (the keychain lookup during auth) run in FastAPI's
  thread pool by being declared as plain `def` dependencies.

**Where.** Everything in `services/`, `db/` and `api/` is async. `asyncio.wait_for` and
`asyncio.create_task` appear in `services/proc.py`.

## Context managers and async with

**What.** A context manager runs setup code, hands you something, and **guarantees** cleanup code
runs afterwards, even on an exception. `with open(...) as f:` is the familiar one. `async with` is
the same idea when setup or cleanup needs `await`.

`@asynccontextmanager` turns a generator into one: code before `yield` is setup, code after it is
cleanup.

```python
@asynccontextmanager
async def session_scope(factory):          # gaia/db/engine.py
    async with factory() as session:
        try:
            yield session                  # the caller's `async with` body runs here
        except BaseException:
            await session.rollback()       # any failure: undo
            raise
        else:
            await session.commit()         # success: make it permanent
```

**Where.** `session_scope` (a database transaction), the FastAPI **lifespan** in `api/app.py`
(startup before `yield`, shutdown after), and `get_session` in `api/deps.py`.

## Dependency injection in FastAPI

**What.** Instead of a route building its own database session or checking auth itself, it
*declares* what it needs, and FastAPI calls the providers ("dependencies") for it:

```python
@router.get("/me")
async def me(principal: PrincipalDep) -> Principal:   # PrincipalDep = Annotated[Principal, Depends(get_principal)]
    return principal
```

FastAPI sees `Depends(get_principal)`, calls `get_principal(request)` first, and passes the result
in. Dependencies can depend on other dependencies (`get_scoped` needs `get_session` and
`get_principal`). Each is called once per request and then cached.

**Dependencies with `yield`** work like context managers. `get_session` opens a session, `yield`s it
to the route, and commits afterwards. By default ("request" scope) the code after `yield` runs
**after the response has been sent**. With `Depends(..., scope="function")` it runs before. See
review finding F6 for why that matters.

**Router-level dependencies.** `api_v1_router()` attaches `Depends(get_principal)` to the whole
router, so every route under `/api/v1` is authenticated even if its author forgets.

## Protocols

**What.** A `typing.Protocol` describes an interface by its *shape*: "anything with these methods".
A class satisfies it without inheriting from it. This is "structural typing", or duck typing that
mypy can check.

```python
class SecretStore(Protocol):                 # gaia/config/secrets.py
    def get(self, name: SecretName) -> SecretStr | None: ...
    def set(self, name: SecretName, value: str) -> None: ...
```

`KeyringSecretStore` and `InMemorySecretStore` both match it without importing it. Gaia uses
Protocols at every seam that will change later (`SecretStore`, `AuthProvider`, and soon
`AgentRuntime`), so a cloud secret manager or OIDC can be dropped in without touching the callers.
`@runtime_checkable` additionally allows `isinstance(obj, SecretStore)` checks in tests.

## Generics with type parameters

**What.** `class ScopedRepo[ModelT: Base]` (Python 3.12 syntax, PEP 695) declares a class that
works for *any* model type, while mypy still knows exactly which one you have:

```python
tickets = ScopedRepo(session, Ticket, workspace_id)   # ScopedRepo[Ticket]
row = await tickets.get(ticket_id)                    # mypy knows: Ticket | None
```

`ModelT: Base` means "any subclass of `Base`". The older spelling is
`ModelT = TypeVar("ModelT", bound=Base)` plus `class ScopedRepo(Generic[ModelT])`. Same meaning,
more boilerplate.

## Dataclasses and frozen objects

`@dataclass(frozen=True)` generates `__init__`, `__repr__` and `__eq__` from the annotated fields,
and makes instances immutable. Gaia uses it for **values that describe a result**, such as
`ProcResult`, `Commit`, `GitWorktree` and `AppServices`, so nothing downstream can modify them by
accident. Pydantic `BaseModel` is used instead where data crosses a boundary and needs
**validation** (API bodies, settings).

## StrEnum

`class TicketStatus(StrEnum)` members *are* strings: `TicketStatus.DONE == "done"` is true. That
lets the database store plain text (portable across SQLite and Postgres) while code still uses
named constants that mypy checks. See [database.md](database.md#why-its-built-this-way).

## Pydantic models and settings

**Models** validate data on the way in: `TicketRead.model_validate(row)` raises if `status` isn't a
valid `TicketStatus`. `ConfigDict(from_attributes=True)` lets a schema be built straight from a
SQLAlchemy row.

**Settings** (`pydantic_settings.BaseSettings`) are models whose fields come from environment
variables. `Settings(env_prefix="GAIA_")` reads `GAIA_PORT` into `port`.

**Validators** run custom checks:
- `@field_validator("data_dir", ...)` transforms one field (expands `~`).
- `@model_validator(mode="after")` checks the whole object (the host must be loopback unless
  `allow_remote`).

**`SecretStr`** wraps a secret so `print()` and `repr()` show `**********`. You must call
`.get_secret_value()` to get the real string, which makes every use of a secret visible in code
review.

## SQLAlchemy sessions, flush and commit

**What.** A *session* is a unit of work. You add or change Python objects, and the session turns
them into SQL.
- **`flush()`** sends pending changes to the database *inside the current transaction*. Defaults and
  generated ids get filled in, but nothing is permanent and other connections can't see it yet.
  Repositories flush so the caller gets a complete row back.
- **`commit()`** ends the transaction and makes everything permanent. Only the outermost owner of
  the transaction commits (`session_scope`, or the request's `get_session`).
- **`rollback()`** throws away everything since the last commit.

`expire_on_commit=False` (in `create_session_factory`) keeps a row's attributes readable after a
commit. Without it, touching `row.status` after `commit()` would trigger a hidden query, and in async
code a hidden query is an error.

**The 2.0 typing style.** `status: Mapped[str] = mapped_column(String(32), default=...)` declares a
column that mypy understands. `Mapped[str | None]` means the column is nullable.

## Alembic migrations

The models describe what the schema *should* be. A migration is a script that moves an existing
database from one version to the next. `alembic revision --autogenerate -m "..."` diffs the models
against the database and drafts the script, which **you then review by hand**. `make migrate`
applies every migration that hasn't run yet. Never edit a migration that has merged: add a new one.
SQLite can't alter tables in place, so migrations use `render_as_batch=True`, which rebuilds the
table behind the scenes.

## structlog processors and context variables

structlog builds each log line as a dict and passes it through a chain of **processors**, functions
that add, change or remove keys, before a renderer prints it. Gaia's chain adds the level and a UTC
timestamp, formats any exception, **redacts secrets**, then renders. Redaction runs after exception
formatting, so traceback text is masked too.

**Context variables** (`structlog.contextvars.bind_contextvars(request_id=...)`) attach a value to
every log line produced while handling the current request, without passing it around. They are
per-task, so concurrent requests don't mix their ids.

## ASGI middleware

FastAPI apps speak **ASGI**: the server calls `app(scope, receive, send)` for every request. A
middleware is an object with the same signature that wraps the app. It can inspect `scope` (path,
headers), and it can wrap `send` to edit the response as it goes out:

```python
async def send_with_headers(message):
    if message["type"] == "http.response.start":     # the status line + headers
        MutableHeaders(scope=message)["X-Content-Type-Options"] = "nosniff"
    await send(message)
```

`app.add_middleware()` inserts at the *front*, so the **last** one added is the **outermost**. See
`api/app.py` for why Gaia's order is security headers → request id → host check.

## Process groups and signals

When Gaia starts a command with `start_new_session=True`, the child becomes the leader of a new
**process group**, and anything it starts joins that group. `os.killpg(pid, SIGKILL)` then kills the
whole tree at once. Killing only the direct child would leave a `pytest` worker or a shell pipeline
behind. `SIGKILL` can't be caught or ignored, so a timed-out command stops for certain. A process
can deliberately leave the group (`setsid()`); see review finding F2 for how Gaia should handle that.

After a child exits it must be **reaped** (`await process.wait()`), otherwise it lingers as a
"zombie" entry in the process table.

## Process-wide singletons

`@functools.lru_cache(maxsize=1)` on a zero-argument function (`get_settings()`) computes the value
once and returns the same object afterwards: a simple, testable singleton. Tests that need different
settings call `Settings(...)` directly or clear the cache with `get_settings.cache_clear()`.

## Constant-time comparison

`token == expected` returns as soon as one character differs. An attacker who can time many
requests could learn a token one character at a time. `hmac.compare_digest(a, b)` takes the same
time whatever the inputs. Gaia compares the bearer token that way, on **bytes**, because
`compare_digest` raises on non-ASCII `str` input, and that would turn a 401 into a 500.

## UUIDv7 identifiers

Every row id is a UUIDv7 string (`gaia/db/ids.py`). Unlike random UUIDv4s, a v7 starts with a
millisecond timestamp, so ids **sort by creation time**. That keeps database index inserts local
and makes `ORDER BY id` mean "oldest first". Within one millisecond the order is random, which is why
the event log uses a separate auto-incrementing `seq` column for strict ordering.

## Git worktrees

One git repository can have several **worktrees**: extra working directories, each with its own
checked-out branch, all sharing one object store. `git worktree add -b gaia/x <path> main` creates
one. Gaia gives each ticket its own worktree under `~/.gaia/worktrees/`, so agents can work on
several tickets in parallel without touching your own checkout.

A worktree's `.git` is a small *file* (`gitdir: <repo>/.git/worktrees/<name>`), not a directory.
That detail matters for security: see review finding F1.

## The OS keychain

`keyring` is a library that talks to the operating system's secret store: the macOS Keychain, or
Secret Service on Linux. Secrets are encrypted at rest by the OS, unlocked with your login, and
never written to a file Gaia manages. In tests, `tests/conftest.py` swaps in an in-memory backend
so nothing touches your real keychain.
