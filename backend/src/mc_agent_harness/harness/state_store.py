from typing import Any, Protocol


class StateStore(Protocol):
    """Persistence contract for checkpoints and recovery state."""

    async def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        """Persist a checkpoint for a run."""

        ...

    async def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Load the latest checkpoint for a run if one exists."""

        ...
