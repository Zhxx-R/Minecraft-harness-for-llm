import pytest

from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.runtime.server_commands import ServerCommandResult
from mc_agent_harness.runtime.threat_pause import ThreatPauseConfig, ThreatPauseRuntime
from mc_agent_harness.schemas.action import HarnessAction


@pytest.mark.anyio
async def test_threat_pause_freezes_only_during_observation() -> None:
    """A hostile ReAct observation should freeze ticks and expose pause audit metadata."""

    wrapped = FakeRuntime([hostile_observation("skeleton", distance=7)])
    executor = ToggleFreezeExecutor()
    runtime = ThreatPauseRuntime(
        wrapped,
        executor=executor,
        config=ThreatPauseConfig(enabled=True, threat_distance=16),
    )

    observation = await runtime.observe()
    second_observation = await runtime.observe()

    assert observation["threat_pause"]["should_pause"] is True
    assert observation["threat_pause"]["already_paused"] is False
    assert observation["threat_pause"]["threats"][0]["name"] == "skeleton"
    assert second_observation["threat_pause"]["already_paused"] is True
    assert executor.commands == ["tick freeze"]
    assert executor.paused is True
    assert wrapped.observe_count == 2


@pytest.mark.anyio
async def test_threat_pause_unfreezes_before_mutating_action_without_post_action_observe() -> None:
    """World-changing actions should unfreeze, but action execution should not trigger observation."""

    wrapped = FakeRuntime([hostile_observation("zombie", distance=4)])
    executor = ToggleFreezeExecutor()
    runtime = ThreatPauseRuntime(
        wrapped,
        executor=executor,
        config=ThreatPauseConfig(enabled=True, threat_distance=16),
    )

    await runtime.observe()
    result = await runtime.act(HarnessAction(type="move_to", args={"x": 1, "y": 64, "z": 1}))

    assert executor.commands == ["tick freeze", "tick freeze"]
    assert executor.paused is False
    assert "threat_pause_before_action" in result
    assert "threat_pause_after_action" not in result
    assert wrapped.observe_count == 1
    assert wrapped.actions == ["move_to"]


@pytest.mark.anyio
async def test_threat_pause_keeps_read_only_action_frozen() -> None:
    """Frozen-safe inspection actions should not advance time before model deliberation is done."""

    wrapped = FakeRuntime([hostile_observation("skeleton", distance=5)])
    executor = ToggleFreezeExecutor()
    runtime = ThreatPauseRuntime(
        wrapped,
        executor=executor,
        config=ThreatPauseConfig(enabled=True, threat_distance=16),
    )

    await runtime.observe()
    result = await runtime.act(HarnessAction(type="query_inventory", args={}))

    assert executor.commands == ["tick freeze"]
    assert executor.paused is True
    assert "threat_pause_before_action" not in result
    assert wrapped.observe_count == 1
    assert wrapped.actions == ["query_inventory"]


@pytest.mark.anyio
async def test_threat_pause_ignores_passive_entities() -> None:
    """Passive nearby entities should not freeze the world."""

    wrapped = FakeRuntime([hostile_observation("chicken", distance=2)])
    executor = ToggleFreezeExecutor()
    runtime = ThreatPauseRuntime(
        wrapped,
        executor=executor,
        config=ThreatPauseConfig(enabled=True, threat_distance=16),
    )

    observation = await runtime.observe()

    assert observation["threat_pause"]["should_pause"] is False
    assert executor.commands == []
    assert executor.paused is False


@pytest.mark.anyio
@pytest.mark.parametrize("entity_name", ["enderman", "spider", "zombified_piglin"])
async def test_threat_pause_ignores_neutral_or_conditionally_hostile_entities(entity_name: str) -> None:
    """Neutral or conditionally hostile nearby entities should not freeze during observation."""

    wrapped = FakeRuntime([hostile_observation(entity_name, distance=4)])
    executor = ToggleFreezeExecutor()
    runtime = ThreatPauseRuntime(
        wrapped,
        executor=executor,
        config=ThreatPauseConfig(enabled=True, threat_distance=16),
    )

    observation = await runtime.observe()

    assert observation["threat_pause"]["should_pause"] is False
    assert executor.commands == []
    assert executor.paused is False


class ToggleFreezeExecutor:
    """In-memory Carpet-style tick-freeze executor for threat pause tests."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.paused = False

    async def execute_many(self, commands) -> list[ServerCommandResult]:
        """Toggle paused state for each tick freeze command."""

        results: list[ServerCommandResult] = []
        for command in commands:
            self.commands.append(command)
            if command == "tick freeze":
                self.paused = not self.paused
                response = "Game is frozen" if self.paused else "Game runs normally"
            else:
                response = "ok"
            results.append(ServerCommandResult(command=command, ok=True, response=response))
        return results


class FakeRuntime(GameRuntime):
    """Minimal runtime that records observe and action calls."""

    def __init__(self, observations: list[dict[str, object]]) -> None:
        self.observations = observations
        self.observe_count = 0
        self.actions: list[str] = []

    async def reset(self, task_spec: dict[str, object]) -> dict[str, object]:
        """Return fake reset metadata."""

        return {"ok": True, "task_id": task_spec.get("task_id")}

    async def observe(self) -> dict[str, object]:
        """Return the configured observation without mutating world state."""

        index = min(self.observe_count, len(self.observations) - 1)
        self.observe_count += 1
        return dict(self.observations[index])

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Record an action and return a fake action result."""

        self.actions.append(action.type)
        return {"ok": True, "action_type": action.type}

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""


def hostile_observation(name: str, *, distance: float) -> dict[str, object]:
    """Build a compact observation containing one nearby entity."""

    return {
        "nearby_entities": [
            {
                "id": 1,
                "name": name,
                "type": name,
                "distance": distance,
                "position": {"x": 1, "y": 64, "z": 1},
                "line_of_sight": True,
                "target_airborne": False,
                "dropped_item": None,
            }
        ],
        "inventory": [],
    }
