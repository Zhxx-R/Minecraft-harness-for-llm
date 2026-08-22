from typing import Any, Protocol

from mc_agent_harness.schemas.action import HarnessAction


class GameRuntime(Protocol):
    """Runtime adapter contract for online Minecraft execution."""

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any] | None:
        """Reset the runtime for a task and optionally return reset audit metadata."""

        ...

    async def observe(self) -> dict[str, Any]:
        """Return the current structured game observation."""

        ...

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Execute one validated harness action."""

        ...

    async def snapshot(self) -> dict[str, Any]:
        """Capture a point-in-time runtime snapshot for audit or vision input."""

        ...

    async def close(self) -> None:
        """Close runtime resources for the current session."""

        ...
