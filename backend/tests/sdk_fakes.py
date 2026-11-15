"""Fake Claude SDK client and script steps for 10c tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


@dataclass(frozen=True)
class SdkToolCall:
    """Ask :class:`FakeSdkClient` to invoke one in-process SDK MCP tool."""

    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class SdkBlock:
    """Block ``receive_response`` until ``interrupt`` is called."""


@dataclass(frozen=True)
class SdkResult:
    """Yield a result message and stop, like the SDK's ``receive_response``."""

    message: ResultMessage


def success_result(*, usage: dict[str, Any] | None = None) -> ResultMessage:
    """A minimal successful SDK result for scripted runs."""
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        usage=usage,
    )


def error_result(message: str) -> ResultMessage:
    """A minimal failed SDK result for scripted runs."""
    return ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="test-session",
        errors=[message],
    )


class FakeSdkClient:
    """A scripted :class:`ClientLike` that can invoke the real SDK MCP tools.

    ``script`` may contain raw SDK message objects (yielded directly),
    :class:`SdkToolCall` (executed through the runtime's real tool handler),
    :class:`SdkBlock` (block until :meth:`interrupt`), or :class:`SdkResult`.
    """

    def __init__(
        self,
        options: Any,
        script: Sequence[Any],
        *,
        runtime: Any = None,
    ) -> None:
        self.options = options
        self.script = list(script)
        self.runtime = runtime
        self.task_id = runtime.active_task_id if runtime is not None else ""
        self.connected = False
        self.queries: list[str] = []
        self.interrupted = False
        self._block = asyncio.Event()

    async def __aenter__(self) -> FakeSdkClient:
        self.connected = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.connected = False
        return False

    async def query(self, prompt: str, session_id: str = "default") -> None:
        del session_id
        self.queries.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True
        self._block.set()

    async def receive_response(self) -> AsyncIterator[Any]:
        """Yield messages and execute tool-call steps in script order."""
        for step in self.script:
            if isinstance(step, SdkToolCall):
                await self._invoke_tool(step)
            elif isinstance(step, SdkBlock):
                await self._block.wait()
            elif isinstance(step, SdkResult):
                yield step.message
                return
            else:
                yield step
                if isinstance(step, ResultMessage):
                    return

    async def _invoke_tool(self, step: SdkToolCall) -> None:
        if self.runtime is None or not self.task_id:
            raise RuntimeError("FakeSdkClient is not attached to a runtime task")
        sdk_tool = self.runtime.sdk_tool(self.task_id, step.name)
        await sdk_tool.handler(dict(step.arguments))


def assistant_text(text: str, *, model: str = "test-model") -> AssistantMessage:
    """Build a scripted assistant text message."""
    return AssistantMessage(model=model, content=[TextBlock(text=text)])
