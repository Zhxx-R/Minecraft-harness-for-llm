from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mc_agent_harness.db.models import CheckpointRecord, RunRecord
from mc_agent_harness.db.session import SessionFactory


class DatabaseStateStore:
    """SQL-backed state store for checkpoint and resume state."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    async def save_checkpoint(self, run_id: str, state: dict[str, Any]) -> None:
        """Persist a checkpoint for a run."""

        step_index = int(state.get("step_index", state.get("next_step_index", 0)))
        with self.session_factory() as session:
            if session.get(RunRecord, run_id) is None:
                session.add(
                    RunRecord(
                        id=run_id,
                        task_id=str(state.get("task_id", "unknown")),
                        status="running",
                        task_spec=state.get("task_spec", {}),
                    )
                )
            session.add(CheckpointRecord(run_id=run_id, step_index=step_index, state=state))
            session.commit()

    async def load_checkpoint(self, run_id: str) -> dict[str, Any] | None:
        """Load the latest checkpoint for a run if one exists."""

        with self.session_factory() as session:
            checkpoint = session.scalar(
                select(CheckpointRecord)
                .where(CheckpointRecord.run_id == run_id)
                .order_by(CheckpointRecord.id.desc())
                .limit(1)
            )
            return dict(checkpoint.state) if checkpoint is not None else None
