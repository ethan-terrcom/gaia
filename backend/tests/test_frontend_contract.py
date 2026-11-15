"""Values the frontend hard-codes must match the backend's (lead review 16, U1).

The UI subscribes to each SSE event by name, because a browser delivers named
events only to listeners for that name. If a new ``EventType`` is added and the
frontend's list is not updated, the UI would silently never see it.
"""

from __future__ import annotations

import json
from pathlib import Path

from gaia.domain.enums import EventType

FRONTEND_API = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api"


def test_frontend_event_types_match_the_backend_enum() -> None:
    listed = json.loads((FRONTEND_API / "event-types.json").read_text(encoding="utf-8"))
    assert listed == [event_type.value for event_type in EventType], (
        "frontend/src/api/event-types.json is out of date; regenerate it from EventType"
    )
