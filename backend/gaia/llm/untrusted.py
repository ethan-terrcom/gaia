"""Prompt-injection wrapping for repo and ticket content (doc 07 security)."""

from __future__ import annotations

from html import escape


def wrap_untrusted(text: str, source: str) -> str:
    """Wrap ``text`` in an ``<untrusted_content>`` element.

    ``source`` is a short label such as ``repo:README.md`` or ``ticket:12``. It
    is escaped before being placed in the attribute so a hostile filename cannot
    break out of the element. The text itself is deliberately left unchanged:
    the model needs the real file/ticket content, and the system prompt (doc 11)
    tells it that instructions inside the element are data, not commands.
    """
    return (
        f'<untrusted_content source="{escape(source, quote=True)}">\n{text}\n</untrusted_content>'
    )
