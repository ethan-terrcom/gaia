"""structlog configuration.

Owned by doc 02; doc 18b extends it with JSON log files and rotation. The only
thing this module must never lose is the redaction processor's position: after
``format_exc_info`` (so traceback text is masked) and immediately before the
renderer (so nothing can print an event once redaction ran).
"""

from __future__ import annotations

import io
import logging
import sys
from typing import TextIO, cast

import structlog

from gaia.security.redaction import redaction_processor


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


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> None:
    """Install structlog with redaction, at ``level``.

    ``stream`` defaults to the current stderr; tests pass a ``StringIO`` to assert on the
    rendered output, which is the only way to prove the processor chain (and
    therefore redaction) actually runs.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redaction_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(
            # A TextIOBase subclass is a text stream at runtime; typing only
            # knows the TextIO protocol, hence the cast.
            file=stream if stream is not None else cast("TextIO", _CurrentStderr())
        ),
        # Tests reconfigure logging; without this a logger built earlier would
        # keep the old chain.
        cache_logger_on_first_use=False,
    )
    # TODO(gaia-18b): add JSON log files, rotation and an events bridge.
