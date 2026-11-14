"""The single decision type shared by the path and command policies.

Security-critical code should have one word for an answer: ``allow``, ``deny``
or ``ask``, plus a reason string that the audit log (doc 09b) can write down
without interpreting anything. Keeping the type here avoids a dependency
between :mod:`gaia.security.paths` and :mod:`gaia.security.commands`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["allow", "deny", "ask"]


@dataclass(frozen=True)
class Decision:
    """An immutable policy decision.

    ``action`` is exactly one of ``"allow"``, ``"deny"`` or ``"ask"``. Callers
    must never treat an unknown future value as ``allow``; the rule engine only
    ever constructs this class, so the closed :data:`Action` type is the
    guarantee.
    """

    action: Action
    reason: str
