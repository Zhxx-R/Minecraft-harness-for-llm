from typing import Any, Protocol


class TaskProvider(Protocol):
    """Contract for task sources such as MineDojo manifests."""

    async def list_tasks(self) -> list[dict[str, Any]]:
        """List available task metadata."""

        ...

    async def load_task(self, task_id: str) -> dict[str, Any]:
        """Load a full task specification."""

        ...

    async def verify(self, run_state: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a run state against the task success criteria."""

        ...
