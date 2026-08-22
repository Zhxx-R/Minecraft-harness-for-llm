from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mc_agent_harness.harness.evaluation import EvaluationRecorder, RecordedEvent
from mc_agent_harness.harness.execution_loop import ExecutionBudget, ExecutionLoop, ExecutionRunResult
from mc_agent_harness.harness.tool_registry import CANONICAL_PRIMITIVE_ACTIONS, ToolRegistry
from mc_agent_harness.models.router import ModelCompletion, ModelProfile, ModelRouter, ModelUsage
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    """Configuration shared by all tasks in one Week 6 benchmark run."""

    model_profile: str = "scripted-week6"
    runtime_profile: str = "benchmark-minimal"
    seed: int = 20260624
    max_steps: int | None = None
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0


@dataclass(frozen=True, slots=True)
class BenchmarkTaskResult:
    """Metrics and audit summary for one benchmark task run."""

    task_id: str
    category: str
    success: bool
    verifier: dict[str, Any]
    run_id: str | None
    steps: int
    duration_sec: float
    invalid_action_count: int
    runtime_error_count: int
    runtime_crashed: bool
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Aggregate benchmark result ready for JSON and Markdown export."""

    benchmark_id: str
    model_profile: str
    runtime_profile: str
    seed: int
    task_count: int
    success_count: int
    success_rate: float
    invalid_action_rate: float
    runtime_crash_rate: float
    total_steps: int
    total_duration_sec: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    estimated_cost: float
    tasks: list[BenchmarkTaskResult]


class ScriptedActionProvider:
    """Model provider that emits task manifest scripted actions deterministically."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = list(actions)
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Return the next scripted action as a JSON completion."""

        _ = (messages, profile, response_schema)
        if not self.actions:
            action = {"type": "query_inventory", "args": {}}
        else:
            action = self.actions[min(self.calls, len(self.actions) - 1)]
        self.calls += 1
        return ModelCompletion(
            content=json.dumps(action, sort_keys=True),
            usage=ModelUsage(input_tokens=0, output_tokens=0, total_tokens=0),
            raw_response={"source": "scripted_week6"},
        )


class ScriptedBenchmarkRuntime(GameRuntime):
    """In-memory Minecraft-like runtime for deterministic Week 6 benchmark tests."""

    RANGED_WEAPONS = frozenset({"bow", "crossbow", "trident"})
    DROP_MAP = {"stone": "cobblestone", "grass_block": "dirt"}
    RECIPES = {
        "oak_planks": {"output": 4, "inputs": {"oak_log": 1}, "station": None},
        "crafting_table": {"output": 1, "inputs": {"oak_planks": 4}, "station": None},
        "stick": {"output": 4, "inputs": {"oak_planks": 2}, "station": None},
        "wooden_pickaxe": {
            "output": 1,
            "inputs": {"oak_planks": 3, "stick": 2},
            "station": "crafting_table",
        },
    }

    def __init__(self) -> None:
        self.task_spec: dict[str, Any] = {}
        self.inventory: dict[str, int] = {}
        self.equipment: dict[str, str | None] = self._empty_equipment()
        self.nearby_blocks: list[dict[str, Any]] = []
        self.nearby_entities: list[dict[str, Any]] = []
        self.position: dict[str, float] = {"x": 0, "y": 65, "z": 0}
        self.active_follow: dict[str, Any] | None = None
        self.closed = False

    async def reset(self, task_spec: dict[str, Any]) -> None:
        """Reset in-memory state from the manifest benchmark initial state."""

        self.task_spec = task_spec
        initial_state = _benchmark_section(task_spec).get("initial_state", {})
        self.inventory = _inventory_counts(initial_state.get("inventory", []))
        self.equipment = self._initial_equipment(initial_state.get("equipment"))
        self.nearby_blocks = [dict(block) for block in initial_state.get("nearby_blocks", [])]
        self.nearby_entities = [dict(entity) for entity in initial_state.get("nearby_entities", [])]
        self.position = dict(initial_state.get("position", self.position))
        self.active_follow = None
        self.closed = False

    async def observe(self) -> dict[str, Any]:
        """Return a structured observation compatible with ProgrammaticVerifier."""

        return self._observation()

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Execute one deterministic high-level action against in-memory state."""

        stopped_follow = None
        if action.type != "follow" and self.active_follow is not None:
            stopped_follow = {
                **self.active_follow,
                "active": False,
                "stop_reason": "next_action_received",
            }
            self.active_follow = None

        if action.type == "query_inventory":
            result = self._success(action.type, {"inventory": self._inventory_list()})
        elif action.type == "scan_blocks":
            result = self._scan_blocks(action)
        elif action.type == "scan_entities":
            result = self._scan_entities(action)
        elif action.type == "scan_dropped_items":
            result = self._scan_dropped_items(action)
        elif action.type == "move_to":
            result = self._move_to(action)
        elif action.type == "follow":
            result = self._follow(action)
        elif action.type == "dig_block_at":
            result = self._dig_block_at(action)
        elif action.type == "wait_ticks":
            result = self._wait_ticks(action)
        elif action.type == "craft_item":
            result = self._craft_item(action)
        elif action.type == "place_block":
            result = self._place_block(action)
        elif action.type == "equip_item":
            result = self._equip_item(action)
        elif action.type == "consume_item":
            result = self._consume_item(action)
        elif action.type in {"move_to_and_engage_combat", "engage_combat"}:
            result = self._engage_combat(action)
        elif action.type == "fight_entity":
            result = self._fight_entity(action)
        else:
            result = self._failure(
                action.type,
                "unsupported_action",
                f"Unsupported scripted action: {action.type}",
            )
        if stopped_follow is not None:
            result["persistent_follow_stopped"] = stopped_follow
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot placeholder for benchmark audit."""

        return {"image": None, "format": None, "observation": self._observation()}

    async def close(self) -> None:
        """Mark the scripted runtime closed."""

        self.closed = True

    def _scan_blocks(self, action: HarnessAction) -> dict[str, Any]:
        """Return matching nearby blocks without mutating scripted state."""

        block_name = str(action.args.get("block") or action.args.get("block_id") or action.args.get("name") or "")
        count = max(1, int(action.args.get("count", 16)))
        blocks = [
            {
                **dict(block),
                "distance": _distance(self.position, _position_dict(block.get("position"))),
                "can_dig": True,
            }
            for block in self.nearby_blocks
            if not block_name or block.get("name") == block_name
        ][:count]
        return self._success(action.type, {"query": block_name or None, "blocks": blocks})

    def _scan_dropped_items(self, action: HarnessAction) -> dict[str, Any]:
        """Return matching scripted dropped item entities without mutating state."""

        item_name = str(action.args.get("item") or action.args.get("item_id") or action.args.get("name") or "")
        count = max(1, int(action.args.get("count", 16)))
        dropped_items = [
            {
                "entity_id": entity.get("id"),
                "item": entity.get("dropped_item", {}).get("name"),
                "count": entity.get("dropped_item", {}).get("count", 1),
                "position": entity.get("position"),
                "distance": _distance(self.position, _position_dict(entity.get("position"))),
            }
            for entity in self.nearby_entities
            if entity.get("name") == "item"
            and isinstance(entity.get("dropped_item"), dict)
            and (not item_name or entity["dropped_item"].get("name") == item_name)
        ][:count]
        return self._success(
            action.type,
            {
                "query": item_name or None,
                "dropped_items": dropped_items,
            },
        )

    def _scan_entities(self, action: HarnessAction) -> dict[str, Any]:
        """Return matching scripted entities with combat affordance evidence."""

        entity_name = str(action.args.get("entity") or action.args.get("entity_id") or action.args.get("name") or "")
        count = max(1, int(action.args.get("count", 16)))
        entities = [
            self._entity_evidence(entity)
            for entity in self.nearby_entities
            if entity.get("name") != "item"
            and (
                not entity_name
                or entity.get("name") == entity_name
                or entity.get("type") == entity_name
                or str(entity.get("id")) == entity_name
            )
        ][:count]
        return self._success(action.type, {"query": entity_name or None, "entities": entities})

    def _move_to(self, action: HarnessAction) -> dict[str, Any]:
        """Move scripted position to a requested nearby coordinate."""

        position = action.args.get("position") if isinstance(action.args.get("position"), dict) else action.args
        target = _position_dict(position)
        if target is None:
            return self._failure(action.type, "invalid_args", "move_to requires args.position or x/y/z.")
        self.position = dict(target)
        return self._success(action.type, {"target": dict(target), "distance": 0.0})

    def _follow(self, action: HarnessAction) -> dict[str, Any]:
        """Start a persistent scripted follow that ends on the next action."""

        raw_target = action.args.get("entity_id")
        entity_name = str(
            action.args.get("entity")
            or action.args.get("name")
            or raw_target
            or ""
        )
        target = next(
            (
                entity
                for entity in self.nearby_entities
                if entity.get("name") == entity_name
                or entity.get("type") == entity_name
                or str(entity.get("id")) == str(raw_target)
            ),
            None,
        )
        if target is None:
            return self._failure(
                action.type,
                "target_not_found",
                f"No scripted {entity_name or 'target'} entity exists.",
            )

        replaced_follow = (
            {
                **self.active_follow,
                "active": False,
                "stop_reason": "follow_replaced",
            }
            if self.active_follow is not None
            else None
        )
        evidence = self._entity_evidence(target)
        self.active_follow = {
            "active": True,
            "target": {
                "id": target.get("id"),
                "name": target.get("name"),
                "type": target.get("type"),
                "position": target.get("position"),
            },
            "follow_distance": float(action.args.get("follow_distance", 1.25)),
            "until": "next_action_received",
        }
        return self._success(
            action.type,
            {
                "status": "following",
                "persistent": True,
                "until": "next_action_received",
                "target": evidence,
                "follow_distance": self.active_follow["follow_distance"],
                "active_follow": dict(self.active_follow),
                "replaced_follow": replaced_follow,
                "recommended_next_actions": [
                    (
                        "use_item: Use when the task requires using the held item "
                        "on the followed entity."
                    ),
                    (
                        "move_to_and_engage_combat: Use when the task requires "
                        "attacking the followed entity."
                    ),
                ],
            },
        )

    def _dig_block_at(self, action: HarnessAction) -> dict[str, Any]:
        """Remove one block at an explicit coordinate and create a dropped item entity."""

        position = action.args.get("position") if isinstance(action.args.get("position"), dict) else action.args
        target = _position_dict(position)
        if target is None:
            return self._failure(action.type, "invalid_args", "dig_block_at requires args.position or x/y/z.")
        expected = str(action.args.get("block") or action.args.get("block_id") or action.args.get("name") or "")
        index = next(
            (
                idx
                for idx, block in enumerate(self.nearby_blocks)
                if _same_position(_position_dict(block.get("position")), target)
            ),
            None,
        )
        if index is None:
            return self._failure(action.type, "target_not_found", "No scripted block exists at target.", {"position": target})
        block = self.nearby_blocks[index]
        block_name = str(block.get("name"))
        if expected and block_name != expected:
            return self._failure(
                action.type,
                "unexpected_block",
                f"Expected {expected}, found {block_name}.",
                {"position": target, "actual_block": block_name},
            )
        self.nearby_blocks.pop(index)
        drop = self.DROP_MAP.get(block_name, block_name)
        self.nearby_entities.append(
            {
                "name": "item",
                "type": "object",
                "dropped_item": {"name": drop, "count": 1},
                "position": dict(target),
            }
        )
        return self._success(
            action.type,
            {
                "block": block_name,
                "position": dict(target),
                "block_before": block_name,
                "block_after": "air",
                "inventory_delta": {},
            },
        )

    def _wait_ticks(self, action: HarnessAction) -> dict[str, Any]:
        """Simulate a short wait and automatically pick up drops at the current position."""

        ticks = max(1, int(action.args.get("ticks", 10)))
        inventory_delta: dict[str, int] = {}
        remaining_entities: list[dict[str, Any]] = []
        for entity in self.nearby_entities:
            if entity.get("name") != "item" or not isinstance(entity.get("dropped_item"), dict):
                remaining_entities.append(entity)
                continue
            entity_position = _position_dict(entity.get("position"))
            if _distance(self.position, entity_position) > 1.25:
                remaining_entities.append(entity)
                continue
            dropped = dict(entity["dropped_item"])
            name = str(dropped["name"])
            count = int(dropped.get("count", 1))
            self.inventory[name] = self.inventory.get(name, 0) + count
            inventory_delta[name] = inventory_delta.get(name, 0) + count
        self.nearby_entities = remaining_entities
        return self._success(
            action.type,
            {
                "waited_ticks": ticks,
                "inventory_delta": inventory_delta,
            },
        )

    def _craft_item(self, action: HarnessAction) -> dict[str, Any]:
        """Craft a supported recipe using desired output-count semantics."""

        item_name = str(action.args.get("item") or action.args.get("item_id") or action.args.get("name") or "")
        desired_count = max(1, int(action.args.get("count", 1)))
        recipe = self.RECIPES.get(item_name)
        if recipe is None:
            return self._failure(action.type, "unknown_item", f"Unsupported scripted recipe: {item_name}.")
        station = recipe.get("station")
        if station and not self._has_nearby_block(str(station)):
            return self._failure(action.type, "missing_station", f"No nearby {station} found.", {"item": item_name})

        output_count = int(recipe["output"])
        craft_count = math.ceil(desired_count / output_count)
        required = {name: int(count) * craft_count for name, count in dict(recipe["inputs"]).items()}
        missing = {
            name: required_count
            for name, required_count in required.items()
            if self.inventory.get(name, 0) < required_count
        }
        if missing:
            return self._failure(
                action.type,
                "recipe_not_available",
                f"Missing ingredients for {item_name}.",
                {"item": item_name, "missing": missing},
            )

        for name, required_count in required.items():
            self.inventory[name] -= required_count
            if self.inventory[name] <= 0:
                del self.inventory[name]
        produced = craft_count * output_count
        self.inventory[item_name] = self.inventory.get(item_name, 0) + produced
        return self._success(
            action.type,
            {
                "item": item_name,
                "count": desired_count,
                "craft_count": craft_count,
                "produced_per_craft": output_count,
                "expected_output_count": produced,
                "station": station or "inventory",
            },
        )

    def _place_block(self, action: HarnessAction) -> dict[str, Any]:
        """Place an inventory block into nearby block observations."""

        item_name = str(action.args.get("item") or action.args.get("block") or action.args.get("name") or "")
        if self.inventory.get(item_name, 0) <= 0:
            return self._failure(action.type, "missing_item", f"Inventory does not contain {item_name}.")
        position = action.args.get("position") if isinstance(action.args.get("position"), dict) else {"x": 0, "y": 65, "z": 0}
        self.inventory[item_name] -= 1
        if self.inventory[item_name] <= 0:
            del self.inventory[item_name]
        placed = {"name": item_name, "position": dict(position)}
        self.nearby_blocks.append(placed)
        return self._success(action.type, {"item": item_name, "target": placed["position"]})

    def _equip_item(self, action: HarnessAction) -> dict[str, Any]:
        """Equip one scripted inventory item without activating or consuming it."""

        item_name = str(action.args.get("item") or action.args.get("item_id") or action.args.get("name") or "")
        slot = self._equipment_slot(str(action.args.get("slot") or action.args.get("destination") or "hand"))
        if self.inventory.get(item_name, 0) <= 0:
            return self._failure(
                action.type,
                "missing_item",
                f"Inventory does not contain {item_name}.",
                {
                    "item": item_name,
                    "slot": slot,
                    "equipment": self._equipment_snapshot(),
                    "suggested_next_actions": ["query_inventory"],
                },
            )
        equipment_before = self._equipment_snapshot()
        self.equipment[slot] = item_name
        return self._success(
            action.type,
            {
                "item": item_name,
                "slot": slot,
                "equipment_before": equipment_before,
                "equipment_after": self._equipment_snapshot(),
            },
        )

    def _consume_item(self, action: HarnessAction) -> dict[str, Any]:
        """Consume one scripted inventory item and report state deltas."""

        item_name = str(action.args.get("item") or action.args.get("item_id") or action.args.get("name") or "")
        if self.inventory.get(item_name, 0) <= 0:
            return self._failure(action.type, "missing_item", f"Inventory does not contain {item_name}.")
        self.inventory[item_name] -= 1
        if self.inventory[item_name] <= 0:
            del self.inventory[item_name]
        return self._success(
            action.type,
            {
                "item": item_name,
                "consumed": True,
                "health_before": 20,
                "health_after": 20,
                "health_delta": 0,
                "food_before": 20,
                "food_after": 20,
                "food_delta": 0,
                "inventory_delta": {item_name: -1},
            },
        )

    def _engage_combat(self, action: HarnessAction) -> dict[str, Any]:
        """Remove a matching entity to simulate one bounded combat engagement."""

        entity_name = str(action.args.get("entity") or action.args.get("entity_id") or action.args.get("name") or "")
        mode = str(action.args.get("mode") or "melee")
        index = next(
            (
                idx
                for idx, entity in enumerate(self.nearby_entities)
                if entity.get("name") == entity_name or entity.get("type") == entity_name
            ),
            None,
        )
        if index is None:
            return self._failure(
                action.type,
                "target_lost",
                f"No scripted {entity_name} entity exists.",
                {"entity": entity_name, "mode": mode, "status": "target_lost"},
            )
        entity = self.nearby_entities[index]
        evidence = self._entity_evidence(entity)
        current_weapon = self.equipment.get("main_hand")
        expected_weapon = action.args.get("weapon")
        if isinstance(expected_weapon, str) and expected_weapon and current_weapon != expected_weapon:
            return self._failure(
                action.type,
                "weapon_not_equipped",
                f"Expected {expected_weapon} in main hand, but current main hand is {current_weapon or 'empty'}.",
                {
                    "entity": entity_name,
                    "mode": mode,
                    "status": "weapon_not_equipped",
                    "weapon": expected_weapon,
                    "current_weapon": current_weapon,
                    "equipment": self._equipment_snapshot(),
                    "suggested_next_actions": ["equip_item", "query_inventory"],
                },
            )
        if mode == "ranged" and current_weapon not in self.RANGED_WEAPONS:
            return self._failure(
                action.type,
                "weapon_not_equipped",
                f"Ranged engagement requires a bow, crossbow, or trident in main hand; current main hand is {current_weapon or 'empty'}.",
                {
                    "entity": entity_name,
                    "mode": mode,
                    "status": "weapon_not_equipped",
                    "current_weapon": current_weapon,
                    "equipment": self._equipment_snapshot(),
                    "suggested_modes": ["melee"],
                    "suggested_next_actions": ["equip_item", "query_inventory"],
                },
            )
        if mode == "melee" and evidence.get("target_airborne") and float(evidence.get("distance") or 0) > 3.2:
            return self._failure(
                action.type,
                "target_unreachable",
                "Melee engagement cannot reach the airborne target.",
                {
                    "entity": entity_name,
                    "mode": mode,
                    "status": "target_unreachable",
                    "weapon": current_weapon or "barehand",
                    "equipment": self._equipment_snapshot(),
                    "target": evidence,
                    "suggested_modes": ["ranged"],
                    "suggested_next_actions": [
                        "query_inventory",
                        "equip_item",
                        "move_to_and_engage_combat",
                    ],
                },
            )
        self.nearby_entities.pop(index)
        return self._success(
            action.type,
            {
                "entity": entity_name,
                "mode": mode,
                "status": "target_killed",
                "weapon": current_weapon or "barehand",
                "equipment": self._equipment_snapshot(),
                "attacks": 1 if mode == "melee" else 0,
                "shots": 1 if mode == "ranged" else 0,
                "kill_stat_delta": 1,
                "target": evidence,
            },
        )

    def _fight_entity(self, action: HarnessAction) -> dict[str, Any]:
        """Remove a matching entity to simulate deterministic combat success."""

        entity_name = str(action.args.get("entity") or action.args.get("entity_id") or action.args.get("name") or "")
        index = next((idx for idx, entity in enumerate(self.nearby_entities) if entity.get("name") == entity_name), None)
        if index is None:
            return self._success(action.type, {"entity": entity_name, "defeated": True, "attacks": 0})
        self.nearby_entities.pop(index)
        return self._success(action.type, {"entity": entity_name, "defeated": True, "attacks": 1})

    def _entity_evidence(self, entity: dict[str, Any]) -> dict[str, Any]:
        """Return scripted entity evidence in the same shape as the live worker."""

        position = _position_dict(entity.get("position")) or dict(self.position)
        distance = _distance(self.position, position)
        height_delta = float(position.get("y", 0)) - float(self.position.get("y", 0))
        airborne = bool(entity.get("target_airborne")) or height_delta > 2
        line_of_sight = bool(entity.get("line_of_sight", True))
        melee_reachable = distance <= 3.2 and line_of_sight and not airborne
        return {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "type": entity.get("type"),
            "position": dict(position),
            "distance": distance,
            "height_delta": height_delta,
            "line_of_sight": line_of_sight,
            "target_airborne": airborne,
            "melee_reachable": melee_reachable,
            "suggested_modes": ["melee"] if melee_reachable else ["ranged", "melee"],
        }

    def _success(self, action_type: str, details: dict[str, Any]) -> dict[str, Any]:
        """Build a successful action result with fresh observation."""

        return {"ok": True, "action_type": action_type, **details, "observation": self._observation()}

    def _failure(
        self,
        action_type: str,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a recoverable scripted action failure."""

        return {
            "ok": False,
            "action_type": action_type,
            "error_code": error_code,
            "message": message,
            "recoverable": True,
            **(details or {}),
            "observation": self._observation(),
        }

    def _observation(self) -> dict[str, Any]:
        """Return the current in-memory game state."""

        return {
            "health": 20,
            "food": 20,
            "position": dict(self.position),
            "active_follow": dict(self.active_follow) if self.active_follow is not None else None,
            "inventory": self._inventory_list(),
            "equipment": self._equipment_snapshot(),
            "nearby_blocks": [dict(block) for block in self.nearby_blocks],
            "nearby_entities": [dict(entity) for entity in self.nearby_entities],
        }

    def _inventory_list(self) -> list[dict[str, Any]]:
        """Return inventory counts as observation items."""

        return [{"name": name, "count": count} for name, count in sorted(self.inventory.items()) if count > 0]

    def _empty_equipment(self) -> dict[str, str | None]:
        """Return an empty equipment state keyed by prompt-facing slot names."""

        return {
            "main_hand": None,
            "off_hand": None,
            "head": None,
            "chest": None,
            "legs": None,
            "feet": None,
        }

    def _initial_equipment(self, value: Any) -> dict[str, str | None]:
        """Normalize optional scripted initial equipment from task metadata."""

        equipment = self._empty_equipment()
        if not isinstance(value, dict):
            return equipment
        for raw_slot, raw_item in value.items():
            slot = self._equipment_slot(str(raw_slot))
            item_name = raw_item.get("name") if isinstance(raw_item, dict) else raw_item
            if isinstance(item_name, str) and item_name:
                equipment[slot] = item_name
        return equipment

    def _equipment_snapshot(self) -> dict[str, dict[str, Any] | None]:
        """Return equipment in the same compact shape as the live worker observation."""

        return {slot: {"name": item, "count": 1} if item else None for slot, item in self.equipment.items()}

    def _equipment_slot(self, slot: str) -> str:
        """Normalize action slot aliases to stable state-summary slots."""

        normalized = slot.lower()
        if normalized in {"hand", "mainhand", "main_hand"}:
            return "main_hand"
        if normalized in {"offhand", "off-hand", "off_hand", "shield"}:
            return "off_hand"
        if normalized in {"torso", "chest", "chestplate"}:
            return "chest"
        if normalized in {"head", "helmet"}:
            return "head"
        if normalized in {"legs", "leggings"}:
            return "legs"
        if normalized in {"feet", "boots"}:
            return "feet"
        return "main_hand"

    def _has_nearby_block(self, name: str) -> bool:
        """Return whether the current observation includes a block by name."""

        return any(block.get("name") == name for block in self.nearby_blocks)


class BenchmarkRunner:
    """Runs a fixed task set and exports Week 6 benchmark metrics."""

    def __init__(
        self,
        task_provider: MineDojoTaskProvider,
        config: BenchmarkConfig | None = None,
    ) -> None:
        self.task_provider = task_provider
        self.config = config or BenchmarkConfig()

    async def run(self, task_ids: list[str] | None = None) -> BenchmarkReport:
        """Run selected tasks or every task from the provider."""

        summaries = await self.task_provider.list_tasks()
        selected_ids = task_ids or [str(summary["task_id"]) for summary in summaries]
        tasks: list[BenchmarkTaskResult] = []
        started_at = time.perf_counter()
        for task_id in selected_ids:
            task_spec = await self.task_provider.load_task(task_id)
            tasks.append(await self.run_task(task_spec))

        duration = time.perf_counter() - started_at
        success_count = sum(1 for task in tasks if task.success)
        total_steps = sum(task.steps for task in tasks)
        invalid_actions = sum(task.invalid_action_count for task in tasks)
        runtime_crashes = sum(1 for task in tasks if task.runtime_crashed)
        total_input_tokens = sum(task.input_tokens for task in tasks)
        total_output_tokens = sum(task.output_tokens for task in tasks)
        total_tokens = sum(task.total_tokens for task in tasks)
        estimated_cost = sum(task.estimated_cost for task in tasks)
        task_count = len(tasks)
        return BenchmarkReport(
            benchmark_id=f"week6_{uuid.uuid4().hex[:12]}",
            model_profile=self.config.model_profile,
            runtime_profile=self.config.runtime_profile,
            seed=self.config.seed,
            task_count=task_count,
            success_count=success_count,
            success_rate=(success_count / task_count) if task_count else 0.0,
            invalid_action_rate=(invalid_actions / total_steps) if total_steps else 0.0,
            runtime_crash_rate=(runtime_crashes / task_count) if task_count else 0.0,
            total_steps=total_steps,
            total_duration_sec=duration,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
            tasks=tasks,
        )

    async def run_task(self, task_spec: dict[str, Any]) -> BenchmarkTaskResult:
        """Run one manifest through ExecutionLoop, verifier, and metric extraction."""

        recorder = EvaluationRecorder()
        benchmark = _benchmark_section(task_spec)
        scripted_actions = list(benchmark.get("scripted_actions", []))
        max_steps = self.config.max_steps or int(benchmark.get("max_steps") or len(scripted_actions) or 1)
        runtime = ScriptedBenchmarkRuntime()
        router = ModelRouter(
            default_model=self.config.model_profile,
            provider=ScriptedActionProvider(scripted_actions),
            profiles={
                self.config.model_profile: ModelProfile(
                    id=self.config.model_profile,
                    provider="scripted",
                    tool_json=True,
                )
            },
        )
        loop = ExecutionLoop(
            runtime=runtime,
            model_router=router,
            tool_registry=ToolRegistry(CANONICAL_PRIMITIVE_ACTIONS),
            recorder=recorder,
            budget=ExecutionBudget(max_steps=max_steps, checkpoint_interval_steps=0),
        )

        started_at = time.perf_counter()
        result: ExecutionRunResult | None = None
        runtime_crashed = False
        error: str | None = None
        try:
            result = await loop.run(
                str(task_spec["task_id"]),
                task_spec={
                    **task_spec,
                    "run_id": f"{task_spec['task_id']}_{uuid.uuid4().hex[:8]}",
                    "runtime_profile": self.config.runtime_profile,
                    "benchmark_seed": self.config.seed,
                },
            )
        except Exception as exc:  # noqa: BLE001 - benchmark reports must capture task failures.
            runtime_crashed = True
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await runtime.close()

        duration = time.perf_counter() - started_at
        run_state = _run_state(task_spec, result)
        verifier = await self.task_provider.verify(run_state)
        events = [asdict(event) for event in recorder.events]
        usage = _usage_from_events(recorder.events)
        steps = len(result.steps) if result is not None else 0
        estimated_cost = _estimated_cost(
            usage,
            input_cost_per_1k=self.config.input_cost_per_1k,
            output_cost_per_1k=self.config.output_cost_per_1k,
        )
        return BenchmarkTaskResult(
            task_id=str(task_spec["task_id"]),
            category=str(task_spec.get("category", "")),
            success=bool(verifier.get("success")) and not runtime_crashed,
            verifier=verifier,
            run_id=result.run_id if result else None,
            steps=steps,
            duration_sec=duration,
            invalid_action_count=_count_events(recorder.events, {"invalid_action"}),
            runtime_error_count=_count_events(recorder.events, {"runtime_error"}),
            runtime_crashed=runtime_crashed,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            total_tokens=usage.total_tokens or 0,
            estimated_cost=estimated_cost,
            events=events,
            error=error,
        )


def write_benchmark_report(report: BenchmarkReport, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown benchmark reports to an output directory."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{report.benchmark_id}.json"
    markdown_path = output_dir / f"{report.benchmark_id}.md"
    json_path.write_text(json.dumps(_report_to_json(report), indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_report_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _report_to_json(report: BenchmarkReport) -> dict[str, Any]:
    """Convert dataclass report objects into JSON-safe dictionaries."""

    return {
        **{key: value for key, value in asdict(report).items() if key != "tasks"},
        "tasks": [asdict(task) for task in report.tasks],
    }


def _report_to_markdown(report: BenchmarkReport) -> str:
    """Render a compact Markdown benchmark report."""

    lines = [
        f"# Week 6 Benchmark Report `{report.benchmark_id}`",
        "",
        f"- Model profile: `{report.model_profile}`",
        f"- Runtime profile: `{report.runtime_profile}`",
        f"- Seed: `{report.seed}`",
        f"- Tasks: {report.task_count}",
        f"- Success: {report.success_count}/{report.task_count} ({report.success_rate:.1%})",
        f"- Invalid action rate: {report.invalid_action_rate:.1%}",
        f"- Runtime crash rate: {report.runtime_crash_rate:.1%}",
        f"- Steps: {report.total_steps}",
        f"- Tokens: {report.total_tokens}",
        f"- Estimated cost: {report.estimated_cost:.6f}",
        "",
        "| Task | Category | Success | Steps | Invalid Actions | Runtime Errors | Reason |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for task in report.tasks:
        reason = str(task.verifier.get("reason", task.error or "")).replace("|", "\\|")
        lines.append(
            f"| `{task.task_id}` | {task.category} | {task.success} | {task.steps} | "
            f"{task.invalid_action_count} | {task.runtime_error_count} | {reason} |"
        )
    lines.append("")
    return "\n".join(lines)


def _benchmark_section(task_spec: dict[str, Any]) -> dict[str, Any]:
    """Return a manifest benchmark object or an empty fallback."""

    benchmark = task_spec.get("benchmark")
    return benchmark if isinstance(benchmark, dict) else {}


def _inventory_counts(items: Any) -> dict[str, int]:
    """Convert inventory observation items into count mapping."""

    counts: dict[str, int] = {}
    if not isinstance(items, list):
        return counts
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            counts[item["name"]] = counts.get(item["name"], 0) + int(item.get("count", 1))
    return counts


def _position_dict(value: Any) -> dict[str, float] | None:
    """Normalize position-like dictionaries into x/y/z float coordinates."""

    if not isinstance(value, dict):
        return None
    try:
        return {"x": float(value["x"]), "y": float(value["y"]), "z": float(value["z"])}
    except (KeyError, TypeError, ValueError):
        return None


def _same_position(left: dict[str, float] | None, right: dict[str, float] | None) -> bool:
    """Compare block coordinates using integer block positions."""

    if left is None or right is None:
        return False
    return (
        int(left["x"]) == int(right["x"])
        and int(left["y"]) == int(right["y"])
        and int(left["z"]) == int(right["z"])
    )


def _distance(left: dict[str, float], right: dict[str, float] | None) -> float | None:
    """Return Euclidean distance between two position dictionaries."""

    if right is None:
        return None
    return math.sqrt(
        (left["x"] - right["x"]) ** 2
        + (left["y"] - right["y"]) ** 2
        + (left["z"] - right["z"]) ** 2
    )


def _run_state(task_spec: dict[str, Any], result: ExecutionRunResult | None) -> dict[str, Any]:
    """Build verifier input from an execution result."""

    steps = []
    if result is not None:
        steps = [
            {
                "step_index": step.step_index,
                "observation": step.observation,
                "action": step.action.model_dump(),
                "action_result": step.action_result,
            }
            for step in result.steps
        ]
    return {"task_id": task_spec["task_id"], "task_spec": task_spec, "steps": steps}


def _usage_from_events(events: tuple[RecordedEvent, ...]) -> ModelUsage:
    """Sum token usage from model action events."""

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for event in events:
        if event.event_type != "model_action":
            continue
        usage = event.payload.get("usage", {})
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)
    return ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def _estimated_cost(usage: ModelUsage, input_cost_per_1k: float, output_cost_per_1k: float) -> float:
    """Estimate cost from token usage and per-1k rates."""

    return ((usage.input_tokens or 0) / 1000 * input_cost_per_1k) + (
        (usage.output_tokens or 0) / 1000 * output_cost_per_1k
    )


def _count_events(events: tuple[RecordedEvent, ...], event_types: set[str]) -> int:
    """Count events whose type belongs to a set."""

    return sum(1 for event in events if event.event_type in event_types)
