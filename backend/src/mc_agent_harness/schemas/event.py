from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TrajectoryEvent(BaseModel):
    """Auditable event emitted by model, runtime, verifier, memory, and skill components."""

    run_id: str
    event_type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    agent_id: str | None = None
