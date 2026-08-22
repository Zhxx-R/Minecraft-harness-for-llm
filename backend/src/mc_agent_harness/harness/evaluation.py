from dataclasses import dataclass, field
from typing import Any

from mc_agent_harness.observability.tracing import enrich_trace_payload


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    """One in-memory audit event captured during a harness run."""

    run_id: str
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)


class EvaluationRecorder:
    """Captures standardized trajectories for offline analysis."""

    def __init__(self) -> None:
        self._events: list[RecordedEvent] = []

    @property
    def events(self) -> tuple[RecordedEvent, ...]:
        """Return captured events in append order."""

        return tuple(self._events)

    async def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Record one typed event in the evaluation trajectory stream."""

        self._events.append(
            RecordedEvent(
                run_id=run_id,
                event_type=event_type,
                payload=enrich_trace_payload(run_id, payload),
            )
        )
