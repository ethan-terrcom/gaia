"""A small scriptable ``httpx.MockTransport`` handler for GitHub client tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx


def json_response(
    status_code: int = 200,
    payload: Any = None,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build an httpx response with a JSON body (or an empty JSON body)."""
    return httpx.Response(
        status_code,
        json={} if payload is None else payload,
        headers=headers,
    )


Route = Callable[[httpx.Request], httpx.Response]


class GitHubFake:
    """A route-matching handler for :class:`httpx.MockTransport`.

    Routes are matched in the order they were added, on ``(method, path)`` only:
    query strings are deliberately ignored because the GitHub client sends its
    filters as query params. A route may be a fixed response or a callable, so a
    test can script a sequence of responses for one path. Every request is kept
    in :attr:`calls` so tests can assert on headers, JSON bodies and query strings.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, str, Route | httpx.Response]] = []
        self.calls: list[httpx.Request] = []

    def add(self, method: str, path: str, response: httpx.Response) -> None:
        """Return ``response`` for the next matching ``(method, path)``."""
        self._routes.append((method, path, response))

    def add_dynamic(self, method: str, path: str, handler: Route) -> None:
        """Delegate matching ``(method, path)`` requests to ``handler``."""
        self._routes.append((method, path, handler))

    def add_json(
        self,
        method: str,
        path: str,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Return a JSON response for the next matching ``(method, path)``."""
        self.add(method, path, json_response(status_code, payload, headers=headers))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        # Match the path as sent on the wire, not httpx's decoded ``url.path``:
        # ``gaia%2Fabc`` and ``gaia/abc`` decode alike, and that hid an encoding
        # bug in ``delete_branch`` (lead review G1).
        wire_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
        for method, path, response in self._routes:
            if request.method == method and wire_path == path:
                if isinstance(response, httpx.Response):
                    return response
                return response(request)
        raise AssertionError(
            f"no GitHubFake route for {request.method} {request.url.path} "
            f"(have {[(m, p) for m, p, _ in self._routes]})"
        )


def transport_for(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    """A ``MockTransport`` wrapper that keeps the type annotation at call sites."""
    return httpx.MockTransport(handler)
