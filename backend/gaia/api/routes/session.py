"""The authenticated identity probe (doc 04).

The UI calls it on load to turn a 401 into the "run `gaia ui` to connect" screen
(doc 16), and it is the smallest possible proof that the token, the principal and
the workspace scope are wired end to end.
"""

from __future__ import annotations

from fastapi import APIRouter

from gaia.api.auth import Principal
from gaia.api.deps import PrincipalDep

router = APIRouter(tags=["session"])


@router.get("/me", response_model=Principal, summary="The authenticated caller")
async def me(principal: PrincipalDep) -> Principal:
    """Return the caller the bearer token resolved to."""
    return principal
