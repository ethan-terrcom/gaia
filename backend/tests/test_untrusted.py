"""Prompt-injection wrapping (doc 07 security)."""

from __future__ import annotations

from gaia.llm.untrusted import wrap_untrusted


def test_wrap_untrusted_keeps_content_but_escapes_source() -> None:
    wrapped = wrap_untrusted("print('hi')\n", "repo:README.md")
    assert wrapped == (
        "<untrusted_content source=\"repo:README.md\">\nprint('hi')\n\n</untrusted_content>"
    )


def test_wrap_untrusted_escapes_quote_in_source() -> None:
    wrapped = wrap_untrusted("body", 'ticket:1" onmouseover="x')
    assert 'source="ticket:1&quot; onmouseover=&quot;x"' in wrapped
    assert "body" in wrapped
