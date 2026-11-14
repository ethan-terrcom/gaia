"""Write the OpenAPI document for the frontend type generator.

Run from ``backend/`` with ``uv run python scripts/export_openapi.py`` (or
``make openapi`` from the repository root). The schema is produced offline with
``create_app().openapi()`` because doc 04 deliberately disables ``/openapi.json``
on the live server.
"""

from __future__ import annotations

import json
from pathlib import Path

from gaia.api.app import create_app
from gaia.config.settings import Settings

#: ``backend/scripts`` → repository root → frontend source tree.
TARGET = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "openapi.json"


def main() -> None:
    """Export the schema as sorted, indented JSON for deterministic diffs."""
    document = create_app(Settings()).openapi()
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
