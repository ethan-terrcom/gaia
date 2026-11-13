"""The one route that answers without a token (doc 04 task 5)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class Health(BaseModel):
    """The body of ``GET /api/health``."""

    status: Literal["ok"]


@router.get("/health", response_model=Health, summary="Liveness probe")
async def health() -> Health:
    """Answer without touching the database or the keychain.

    A probe that failed because the database was busy, or the keychain was
    locked, would report the wrong problem. "The process answers HTTP" is what
    this says, and it is the only thing a supervisor needs.
    """
    return Health(status="ok")
