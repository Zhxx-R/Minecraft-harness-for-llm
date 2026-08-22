from mc_agent_harness.schemas.event import TrajectoryEvent


class EventSink:
    """Output boundary for trajectory events."""

    async def emit(self, event: TrajectoryEvent) -> None:
        """Emit one trajectory event to the configured sink."""

        _ = event
