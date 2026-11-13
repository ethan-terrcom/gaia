"""Tests for ``gaia.security.redaction`` (doc 02, task 4).

The fake tokens are assembled from parts on purpose: a credential-shaped
literal must not land in the repository, because gitleaks scans the history.
"""

from __future__ import annotations

import traceback

import pytest

from gaia.security.redaction import (
    MIN_SECRET_LENGTH,
    REDACTED,
    redact,
    redaction_processor,
    register_secret,
)

ANTHROPIC_SHAPED = "sk-ant-" + "test-value-1234"
OPENAI_SHAPED = "sk-" + "a" * 24
GITHUB_CLASSIC_SHAPED = "ghp_" + "a" * 36
GITHUB_FINE_GRAINED_SHAPED = "github_pat_" + "a" * 20
BEARER_TOKEN = "a" * 24
DUMMY_SECRET = "dummy" + "-registered-value"


@pytest.mark.parametrize(
    "token",
    [
        ANTHROPIC_SHAPED,
        OPENAI_SHAPED,
        GITHUB_CLASSIC_SHAPED,
        GITHUB_FINE_GRAINED_SHAPED,
    ],
)
def test_each_token_shape_is_masked(token: str) -> None:
    assert redact(f"before {token} after") == f"before {REDACTED} after"


def test_the_whole_anthropic_key_is_masked_not_just_its_prefix() -> None:
    masked = redact(f"key={ANTHROPIC_SHAPED}")

    assert masked == f"key={REDACTED}"
    assert "test-value-1234" not in masked


def test_a_bearer_token_is_masked_but_the_scheme_word_is_kept() -> None:
    assert redact(f"Authorization: Bearer {BEARER_TOKEN}") == "Authorization: Bearer [REDACTED]"


def test_register_secret_masks_the_literal_everywhere() -> None:
    register_secret(DUMMY_SECRET)

    assert redact(f"first {DUMMY_SECRET} and {DUMMY_SECRET} again") == (
        f"first {REDACTED} and {REDACTED} again"
    )


def test_register_secret_ignores_short_values() -> None:
    short = "a" * (MIN_SECRET_LENGTH - 1)

    register_secret(short)

    assert redact(f"value {short} stays") == f"value {short} stays"
    assert redact(f"but {OPENAI_SHAPED} does not") == f"but {REDACTED} does not"


def test_unregistered_text_is_returned_unchanged() -> None:
    assert redact("a harmless log line") == "a harmless log line"


def test_a_registered_secret_is_masked_before_the_patterns() -> None:
    # A registered value that is also token-shaped is masked either way, but a
    # value with a shape the patterns do not cover must still be masked.
    opaque = "dummy" + "-opaque-value"
    register_secret(opaque)

    assert redact(f"token {opaque}") == f"token {REDACTED}"


def test_the_processor_redacts_nested_values() -> None:
    register_secret(DUMMY_SECRET)

    event = redaction_processor(
        None,
        "info",
        {
            "event": f"using {DUMMY_SECRET}",
            "flat": DUMMY_SECRET,
            "nested": {"inner": [DUMMY_SECRET, "safe"]},
            "sequence": (DUMMY_SECRET, "safe"),
            "count": 3,
            "flag": True,
        },
    )

    assert event["event"] == f"using {REDACTED}"
    assert event["flat"] == REDACTED
    assert event["nested"] == {"inner": [REDACTED, "safe"]}
    assert event["sequence"] == (REDACTED, "safe")
    assert event["count"] == 3
    assert event["flag"] is True
    # Structure is preserved, not flattened into strings.
    assert isinstance(event["nested"], dict)
    assert isinstance(event["nested"]["inner"], list)
    assert isinstance(event["sequence"], tuple)


def test_the_processor_leaves_the_caller_s_event_dict_alone() -> None:
    register_secret(DUMMY_SECRET)
    original = {"event": DUMMY_SECRET}

    redaction_processor(None, "info", original)

    assert original == {"event": DUMMY_SECRET}


def test_a_secret_inside_a_formatted_exception_is_masked() -> None:
    register_secret(DUMMY_SECRET)
    try:
        raise RuntimeError(f"provider rejected {DUMMY_SECRET}")
    except RuntimeError as error:
        formatted = "".join(traceback.format_exception(error))

    event = redaction_processor(None, "error", {"event": "call failed", "exception": formatted})

    assert DUMMY_SECRET not in event["exception"]
    assert REDACTED in event["exception"]
    # The traceback itself survives: redaction must not eat diagnostics.
    assert "RuntimeError" in event["exception"]
