from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.runtime.server_commands import ServerCommandExecutor, ServerCommandResult
from mc_agent_harness.schemas.action import HarnessAction


DEFAULT_HOSTILE_ENTITY_NAMES = (
    # Conservative always-hostile set. Neutral or conditionally hostile mobs are
    # excluded so observation-time freeze does not trigger on non-attacking mobs.
    "blaze",
    "cave_spider",
    "creeper",
    "drowned",
    "elder_guardian",
    "ender_dragon",
    "endermite",
    "evoker",
    "ghast",
    "guardian",
    "hoglin",
    "husk",
    "magma_cube",
    "phantom",
    "piglin_brute",
    "pillager",
    "ravager",
    "shulker",
    "silverfish",
    "skeleton",
    "slime",
    "stray",
    "vex",
    "vindicator",
    "warden",
    "witch",
    "wither",
    "wither_skeleton",
    "zoglin",
    "zombie",
    "zombie_villager",
)


FROZEN_SAFE_ACTIONS = {
    "query_inventory",
    "request_visual_snapshot",
    "scan_blocks",
    "scan_entities",
    "scan_dropped_items",
    "equip_item",
}


@dataclass(frozen=True, slots=True)
class ThreatPauseConfig:
    """Policy for pausing Minecraft ticks during ReAct observation near hostile entities."""

    enabled: bool = False
    threat_distance: float = 16.0
    hostile_entity_names: tuple[str, ...] = DEFAULT_HOSTILE_ENTITY_NAMES
    freeze_command: str = "tick freeze"
    unfreeze_command: str = "tick freeze"
    unfreeze_before_mutating_action: bool = True


@dataclass(frozen=True, slots=True)
class ThreatPauseDecision:
    """Decision and audit payload produced by one threat scan."""

    should_pause: bool
    already_paused: bool
    threats: tuple[dict[str, Any], ...]
    command_results: tuple[ServerCommandResult, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        """Convert the decision into JSON-safe audit metadata."""

        return {
            "should_pause": self.should_pause,
            "already_paused": self.already_paused,
            "threats": list(self.threats),
            "command_results": [result.to_json() for result in self.command_results],
        }


class ThreatPauseRuntime:
    """Runtime wrapper that freezes Minecraft ticks only from ReAct observation."""

    def __init__(
        self,
        runtime: GameRuntime,
        *,
        executor: ServerCommandExecutor,
        config: ThreatPauseConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.executor = executor
        self.config = config or ThreatPauseConfig()
        self._paused = False

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any] | None:
        """Reset the wrapped runtime and clear any stale pause state."""

        if self._paused:
            await self._unfreeze(reason="reset")
        self._paused = False
        reset_result = await self.runtime.reset(task_spec)
        if isinstance(reset_result, dict):
            reset_result["threat_pause"] = {
                "enabled": self.config.enabled,
                "paused": self._paused,
            }
        return reset_result

    async def observe(self) -> dict[str, Any]:
        """Return observation metadata and freeze ticks when hostile entities are nearby."""

        observation = await self.runtime.observe()
        decision = await self._maybe_pause(observation, reason="observe")
        if decision is not None:
            observation["threat_pause"] = decision.to_json()
        return observation

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Execute one action after unfreezing only when the action needs world progression."""

        unfreeze_result = None
        if self._paused and self._should_unfreeze_for_action(action):
            unfreeze_result = await self._unfreeze(reason=f"before_action:{action.type}")

        result = await self.runtime.act(action)
        if unfreeze_result is not None:
            result["threat_pause_before_action"] = [item.to_json() for item in unfreeze_result]
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Capture a runtime snapshot without changing pause state."""

        return await self.runtime.snapshot()

    async def close(self) -> None:
        """Unfreeze the world before closing the wrapped runtime."""

        if self._paused:
            await self._unfreeze(reason="close")
        await self.runtime.close()

    def _should_unfreeze_for_action(self, action: HarnessAction) -> bool:
        """Return whether the action should run with normal ticking enabled."""

        if not self.config.unfreeze_before_mutating_action:
            return False
        return action.type not in FROZEN_SAFE_ACTIONS

    async def _maybe_pause(
        self,
        observation: dict[str, Any],
        *,
        reason: str,
    ) -> ThreatPauseDecision | None:
        """Freeze ticks if the observation contains nearby hostile entities."""

        if not self.config.enabled:
            return None
        hostile_entities = observation.get("nearby_hostile_entities")
        threats = tuple(
            _hostile_threats(
                hostile_entities if isinstance(hostile_entities, list) else observation.get("nearby_entities"),
                hostile_names=self.config.hostile_entity_names,
                max_distance=self.config.threat_distance,
            )
        )
        if not threats:
            return ThreatPauseDecision(
                should_pause=False,
                already_paused=self._paused,
                threats=(),
            )
        if self._paused:
            return ThreatPauseDecision(
                should_pause=True,
                already_paused=True,
                threats=threats,
            )

        results = await self.executor.execute_many([self.config.freeze_command])
        self._paused = _pause_state_from_results(results, fallback=self._paused, intended=True)
        return ThreatPauseDecision(
            should_pause=True,
            already_paused=False,
            threats=threats,
            command_results=tuple(_tag_results(results, reason=reason)),
        )

    async def _unfreeze(self, *, reason: str) -> tuple[ServerCommandResult, ...]:
        """Resume ticking when the wrapper believes the world is paused."""

        results = await self.executor.execute_many([self.config.unfreeze_command])
        self._paused = _pause_state_from_results(results, fallback=self._paused, intended=False)
        return tuple(_tag_results(results, reason=reason))


def _hostile_threats(
    entities: Any,
    *,
    hostile_names: Sequence[str],
    max_distance: float,
) -> Iterable[dict[str, Any]]:
    """Yield hostile nearby entity evidence from a ReAct observation payload."""

    if not isinstance(entities, list):
        return []
    hostile = {name.lower() for name in hostile_names}
    threats: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if entity.get("dropped_item") is not None:
            continue
        names = {
            str(entity.get("name") or "").lower(),
            str(entity.get("type") or "").lower(),
            str(entity.get("display_name") or "").lower(),
        }
        if not names & hostile:
            continue
        distance = _number(entity.get("distance"))
        if distance is None or distance > max_distance:
            continue
        threats.append(
            {
                "id": entity.get("id") or entity.get("entity_id"),
                "name": entity.get("name") or entity.get("type"),
                "type": entity.get("type"),
                "distance": distance,
                "position": entity.get("position"),
                "line_of_sight": entity.get("line_of_sight"),
                "target_airborne": entity.get("target_airborne"),
            }
        )
    return threats


def _tag_results(
    results: Sequence[ServerCommandResult],
    *,
    reason: str,
) -> Iterable[ServerCommandResult]:
    """Attach a threat-pause reason to command responses without hiding command output."""

    for result in results:
        response = result.response
        if response:
            response = f"{response} [threat_pause_reason={reason}]"
        else:
            response = f"[threat_pause_reason={reason}]"
        yield ServerCommandResult(
            command=result.command,
            ok=result.ok,
            response=response,
            error=result.error,
            duration_ms=result.duration_ms,
        )


def _number(value: Any) -> float | None:
    """Return a float for numeric observation fields."""

    return float(value) if isinstance(value, (int, float)) else None


def _pause_state_from_results(
    results: Sequence[ServerCommandResult],
    *,
    fallback: bool,
    intended: bool,
) -> bool:
    """Infer Carpet tick-freeze state from command responses."""

    state = fallback
    if results and all(result.ok for result in results):
        state = intended
    for result in results:
        if not result.ok:
            continue
        normalized = result.response.lower()
        if "game is frozen" in normalized:
            state = True
        elif "game runs normally" in normalized:
            state = False
    return state
