"""structlog configuration.

Owned by doc 02; doc 18b extends it with JSON log files and rotation. The only
thing this module must never lose is the redaction processor's position: after
``format_exc_info`` (so traceback text is masked) and immediately before the
renderer (so nothing can print an event once redaction ran).

Doc 18b routes the standard library's ``logging`` through the same processors:
a ``structlog.stdlib.ProcessorFormatter`` is attached to a handler on the root
logger, and its ``foreign_pre_chain`` runs ``redaction_processor`` after
``format_exc_info``. That is what closes review findings F8 and E4: a
``logging.getLogger("httpx").info(…)`` line and a uvicorn access line are
redacted exactly like a structlog one.

``configure_logging`` is intentionally idempotent about the console handler and
additive about the file handler. ``gaia serve`` installs the file handler
first, then the app lifespan calls ``configure_logging()`` again with no
``log_dir``; that second call must not remove the file handler the first one
installed.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import TextIO, cast

import structlog

from gaia.security.redaction import redaction_processor

#: File mode for the JSON log file: owner read/write only.
LOG_FILE_MODE = 0o600

#: Directory mode for the log directory: owner read/write/execute only.
LOG_DIR_MODE = 0o700

#: Logger that emits uvicorn's request line (``GET /path?ticket=…``).
UVICORN_ACCESS_LOGGER = "uvicorn.access"

#: Handlers this module installed on the root logger, so a repeated call can
#: replace them instead of appending a second handler for the same destination.
_console_handler: logging.Handler | None = None
_file_handler: logging.Handler | None = None
_uvicorn_filter: logging.Filter | None = None


class _CurrentStderr(io.TextIOBase):
    """A stream that writes to whatever ``sys.stderr`` is *now*.

    Logs go to stderr so stdout stays clean for command output (``gaia dev
    plan | jq``). The stream is resolved at write time because binding
    ``sys.stderr`` itself would pin the object of the moment; under pytest that
    is a capture stream that is closed after the test.
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


class _SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that keeps the active file owner-only.

    Python's ``FileHandler`` creates new files with the process umask, which is
    typically ``0o644``. The log file contains every line Gaia emits, so doc 18b
    requires ``0o600``. Rotation renames the active file into ``.1`` (preserving
    its mode) and opens a fresh active file through :meth:`_open`; overriding it
    lets us ``chmod`` that fresh file too.
    """

    def _open(self) -> io.TextIOWrapper:
        """Open the base file ``0o600`` and return a text stream."""
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_FILE_MODE)
        stream = os.fdopen(fd, "a", encoding=self.encoding)
        os.chmod(self.baseFilename, LOG_FILE_MODE)
        return stream


class _UvicornAccessQueryStringFilter(logging.Filter):
    """Drop the query string from uvicorn's access log records.

    Review finding E4: uvicorn's access log includes the full request path with
    its query string, so ``GET /api/v1/stream?ticket=…`` would write the
    single-use stream ticket into the log. The ticket is harmless after use,
    but the whole point of 18b is that no log line can leak a credential-shaped
    value. Rewriting ``record.args`` here keeps the request line useful
    (``GET /api/v1/stream``) without the query.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client_addr, method, full_path, http_version, status_code = args
            if isinstance(full_path, str):
                full_path = full_path.partition("?")[0]
            record.args = (client_addr, method, full_path, http_version, status_code)
        return True


def _remove_handler(handler: logging.Handler | None) -> None:
    """Detach and close a handler we installed on the root logger."""
    if handler is None:
        return
    root = logging.getLogger()
    if handler in root.handlers:
        root.removeHandler(handler)
    handler.close()


def _reset_for_tests() -> None:
    """Remove the process-wide logging state installed by :func:`configure_logging`.

    The structlog configuration and the root handlers are process-wide state;
    tests call this in a fixture so a logging assertion in one test cannot leak
    into the next.
    """
    global _console_handler, _file_handler, _uvicorn_filter

    _remove_handler(_console_handler)
    _console_handler = None
    _remove_handler(_file_handler)
    _file_handler = None
    if _uvicorn_filter is not None:
        logging.getLogger(UVICORN_ACCESS_LOGGER).removeFilter(_uvicorn_filter)
        _uvicorn_filter = None
    structlog.reset_defaults()
    logging.getLogger().setLevel(logging.WARNING)


def _build_formatter(
    renderer: structlog.typing.Processor,
) -> structlog.stdlib.ProcessorFormatter:
    """Return a ``ProcessorFormatter`` with the 18b redaction chain.

    The processors in ``foreign_pre_chain`` are applied to non-structlog
    records before the formatter's own processors. Redaction must come after
    ``format_exc_info`` for foreign records too: a stdlib traceback is text
    like any other and may contain a secret.
    """
    foreign_pre_chain = (
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        redaction_processor,
    )
    return structlog.stdlib.ProcessorFormatter(
        processors=(
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ),
        foreign_pre_chain=foreign_pre_chain,
    )


def _install_console_handler(level: int, stream: TextIO) -> None:
    """Install (or replace) the human-readable handler on the root logger."""
    global _console_handler

    _remove_handler(_console_handler)
    formatter = _build_formatter(structlog.dev.ConsoleRenderer(colors=False))
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    _console_handler = handler


def _install_file_handler(
    level: int,
    log_dir: Path,
    *,
    max_bytes: int,
    backups: int,
) -> None:
    """Install (or replace) the rotating JSON handler on the root logger."""
    global _file_handler

    log_dir.mkdir(mode=LOG_DIR_MODE, parents=True, exist_ok=True)
    log_dir.chmod(LOG_DIR_MODE)

    _remove_handler(_file_handler)
    formatter = _build_formatter(structlog.processors.JSONRenderer())
    handler = _SecureRotatingFileHandler(
        log_dir / "gaia.jsonl",
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    _file_handler = handler


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
    log_dir: Path | None = None,
    max_bytes: int = 10_000_000,
    backups: int = 5,
) -> None:
    """Install structlog and stdlib logging with redaction, at ``level``.

    Every line - structlog's and the standard library's - is processed by
    ``ProcessorFormatter`` on the root logger, so both destinations receive the
    same redacted event. ``stream`` defaults to the current stderr; tests pass a
    ``StringIO`` to assert on the rendered output, which is the only way to
    prove the processor chain (and therefore redaction) actually runs.

    When ``log_dir`` is given, a rotating ``gaia.jsonl`` handler is also
    installed: one redacted JSON object per line. The log directory is forced to
    ``0o700`` and the active file to ``0o600``.
    """
    resolved_stream = stream if stream is not None else cast("TextIO", _CurrentStderr())

    # The structlog side becomes a thin wrapper over stdlib logging. The final
    # processor hands the rendered event dict to the root handler's formatter,
    # which is where console/file rendering happens for both structlog and
    # non-structlog records.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redaction_processor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Tests reconfigure logging; without this a logger built earlier would
        # keep the old chain.
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    root.setLevel(level)
    _install_console_handler(level, resolved_stream)
    if log_dir is not None:
        _install_file_handler(
            level,
            log_dir,
            max_bytes=max_bytes,
            backups=backups,
        )

    # Keep uvicorn's access log but never let it write a query string (E4).
    # Uvicorn's loggers have no handlers after `gaia serve` passes
    # ``log_config=None``; they propagate to the root handler, and the filter on
    # ``uvicorn.access`` rewrites the record before it reaches that handler.
    global _uvicorn_filter
    access_logger = logging.getLogger(UVICORN_ACCESS_LOGGER)
    if _uvicorn_filter is not None:
        access_logger.removeFilter(_uvicorn_filter)
    _uvicorn_filter = _UvicornAccessQueryStringFilter()
    access_logger.addFilter(_uvicorn_filter)

    # TODO(gaia-18b-otel): add optional OpenTelemetry export (doc 18, off by default).
