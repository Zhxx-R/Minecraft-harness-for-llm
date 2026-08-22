from __future__ import annotations

from typing import Any

import pytest

from mc_agent_harness.runtime.visual_snapshot import VisualSnapshotRuntime
from mc_agent_harness.schemas.action import HarnessAction


class StubRuntime:
    """Underlying runtime double used to verify visual-action interception."""

    def __init__(self) -> None:
        self.actions: list[HarnessAction] = []

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Return reset metadata without external state."""

        return {"task_id": task_spec.get("task_id")}

    async def observe(self) -> dict[str, Any]:
        """Return one stable observation attached to action evidence."""

        return {"health": 20, "inventory": []}

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Record delegated non-visual actions."""

        self.actions.append(action)
        return {"ok": True, "action_type": action.type}

    async def snapshot(self) -> dict[str, Any]:
        """Return one structured runtime snapshot."""

        return {"health": 20}

    async def close(self) -> None:
        """Close without external resources."""


class StubFrameProvider:
    """Visual provider double returning one local artifact reference."""

    async def capture(self) -> dict[str, Any]:
        """Return deterministic capture metadata."""

        return {
            "available": True,
            "image": "/tmp/frame.jpg",
            "artifact_path": "/tmp/frame.jpg",
            "format": "jpeg",
        }


@pytest.mark.anyio
async def test_visual_snapshot_runtime_intercepts_only_visual_action() -> None:
    """The visual primitive stays outside worker RPC while normal actions are delegated."""

    underlying = StubRuntime()
    runtime = VisualSnapshotRuntime(underlying, StubFrameProvider())

    visual = await runtime.act(HarnessAction(type="request_visual_snapshot", args={}))
    normal = await runtime.act(HarnessAction(type="query_inventory", args={}))

    assert visual["ok"] is True
    assert visual["snapshot"]["artifact_path"] == "/tmp/frame.jpg"
    assert visual["observation"]["health"] == 20
    assert normal["action_type"] == "query_inventory"
    assert [action.type for action in underlying.actions] == ["query_inventory"]
