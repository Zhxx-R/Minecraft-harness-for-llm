from __future__ import annotations

import asyncio
from typing import Any, Protocol

from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction


class VisualFrameProvider(Protocol):
    """Provider contract for one on-demand visual frame artifact."""

    async def capture(self) -> dict[str, Any]:
        """Capture a frame and return bounded metadata with a local artifact path."""

        ...


class VisualSnapshotRuntime:
    """Runtime decorator that implements visual actions outside Mineflayer JSON-RPC."""

    def __init__(
        self,
        runtime: GameRuntime,
        frame_provider: VisualFrameProvider,
        *,
        readiness_event: asyncio.Event | None = None,
        readiness_timeout_sec: float = 30.0,
    ) -> None:
        self.runtime = runtime
        self.frame_provider = frame_provider
        self.readiness_event = readiness_event
        self.readiness_timeout_sec = max(0.1, readiness_timeout_sec)

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any] | None:
        """Delegate world reset to the underlying runtime."""

        return await self.runtime.reset(task_spec)

    async def observe(self) -> dict[str, Any]:
        """Delegate structured observation to the underlying runtime."""

        return await self.runtime.observe()

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Capture visual actions locally and delegate every other primitive."""

        if action.type != "request_visual_snapshot":
            return await self.runtime.act(action)
        try:
            if self.readiness_event is not None:
                await asyncio.wait_for(
                    self.readiness_event.wait(),
                    timeout=self.readiness_timeout_sec,
                )
            snapshot = await self.frame_provider.capture()
        except Exception as exc:  # noqa: BLE001 - expose capture failure as recoverable evidence.
            observation = await self.runtime.observe()
            return {
                "ok": False,
                "action_type": action.type,
                "error_code": "visual_capture_unavailable",
                "message": f"Visual frame capture failed: {type(exc).__name__}: {exc}",
                "recoverable": True,
                "snapshot": {
                    "image": None,
                    "format": None,
                    "reason": f"{type(exc).__name__}: {exc}",
                },
                "observation": observation,
            }
        observation = await self.runtime.observe()
        return {
            "ok": True,
            "action_type": action.type,
            "snapshot": snapshot,
            "observation": observation,
        }

    async def snapshot(self) -> dict[str, Any]:
        """Delegate non-action runtime snapshots to the underlying runtime."""

        return await self.runtime.snapshot()

    async def close(self) -> None:
        """Close the underlying runtime."""

        await self.runtime.close()
