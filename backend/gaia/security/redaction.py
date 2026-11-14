"""Redaction of secrets in logs and events.

Owned by doc 02 (doc 13 reuses :func:`redact` for event payloads). Two layers:

- a registry of literal values that a :class:`~gaia.config.secrets.SecretStore`
  handed out, so even a value Gaia itself never expects in a log line is masked;
- a fixed set of patterns for the token shapes Gaia knows about, which catches
  keys that never went through a store (a copied ``.env`` line, an error message
  from a provider).

:func:`redaction_processor` is the structlog hook; it walks the whole event dict
so a secret nested inside a dict or a list is masked too.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from structlog.typing import EventDict, WrappedLogger

#: What a masked value is replaced with.
REDACTED = "[REDACTED]"

#: Values shorter than this are not registered: masking a short, common
#: substring would garble unrelated log lines.
MIN_SECRET_LENGTH = 8

# Compiled patterns, in order, as ``(pattern, replacement)``. The replacement is
# an ``re.sub`` template, which is how the Bearer rule keeps the scheme word.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # sk-ant- must come before the generic sk- pattern: an Anthropic key matches
    # both prefixes and the specific rule masks more of it.
    (re.compile(r"sk-ant-[\w-]+"), REDACTED),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"ghp_\w+"), REDACTED),
    (re.compile(r"github_pat_\w+"), REDACTED),
    # Masks the token and keeps the word "Bearer", which is useful in logs.
    (re.compile(r"(?i)(bearer\s+)[\w.~+/=-]{16,}"), rf"\1{REDACTED}"),
)

# Module-level state on purpose: redaction must work from any logger, without
# passing a store around. Tests reset it through _reset_for_tests().
_registered: set[str] = set()


def register_secret(value: str) -> None:
    """Mask ``value`` in every later :func:`redact` call.

    Values shorter than :data:`MIN_SECRET_LENGTH` are ignored rather than
    masked: replacing a short common string everywhere would destroy the logs.
    """
    if len(value) < MIN_SECRET_LENGTH:
        return
    _registered.add(value)


def redact(text: str) -> str:
    """Return ``text`` with registered secrets and known token shapes masked."""
    # Longest first, so a secret that contains another is masked whole.
    for secret in sorted(_registered, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside dicts, lists and tuples.

    ``Any`` is right here: structlog event values are untyped by design, and
    anything that is not a container of strings is returned untouched.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


#: Private name kept for callers that predate doc 13; new code imports
#: :func:`redact_value`.
_redact_value = redact_value


def redaction_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: redact every string in the event dict, recursively."""
    return cast("EventDict", redact_value(dict(event_dict)))


def _reset_for_tests() -> None:
    """Forget every registered value.

    The registry is process-wide state, so tests must not leak into each other;
    ``tests/conftest.py`` calls this around every test.
    """
    _registered.clear()
