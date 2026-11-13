"""structlog configuration.

Owned by doc 02; doc 18b extends it with JSON log files and rotation. The only
thing this module must never lose is the redaction processor's position: after
``format_exc_info`` (so traceback text is masked) and immediately before the
renderer (so nothing can print an event once redaction ran).
"""

from __future__ import annotations

import logging
from typing import TextIO

import structlog

from gaia.security.redaction import redaction_processor


def configure_logging(
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> None:
    """Install structlog with redaction, at ``level``.

    ``stream`` defaults to stdout; tests pass a ``StringIO`` to assert on the
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
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        # Tests reconfigure logging; without this a logger built earlier would
        # keep the old chain.
        cache_logger_on_first_use=False,
    )
    # TODO(gaia-18b): add JSON log files, rotation and an events bridge.
