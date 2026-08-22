from __future__ import annotations

import asyncio
import hashlib
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction


# Global marker used to distinguish harness-generated mobs from natural world entities.
TASK_MOB_GLOBAL_TAG = "mc_agent_task_mob"
# Worker-scoped marker prefix used to avoid cross-worker reset interference.
TASK_MOB_OWNER_TAG_PREFIX = "mc_agent_owner_"


@dataclass(frozen=True, slots=True)
class ServerCommandResult:
    """Auditable result for one server-side Minecraft command."""

    command: str
    ok: bool
    response: str = ""
    error: str | None = None
    duration_ms: float | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert the command result into a JSON-safe audit payload."""

        return {
            "command": self.command,
            "ok": self.ok,
            "response": self.response,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class ServerCommandExecutor(Protocol):
    """Executor contract for privileged server-side Minecraft commands."""

    async def execute_many(self, commands: Sequence[str]) -> list[ServerCommandResult]:
        """Execute commands on the Minecraft server and return auditable results."""

        ...


@dataclass(frozen=True, slots=True)
class ServerCommandResetConfig:
    """Policy for server-authorized reset commands owned by the harness."""

    enabled: bool = False
    clear_inventory: bool = True
    clear_dropped_items: bool = True
    restore_player_state: bool = True
    apply_task_reset_plan: bool = True
    align_biome: bool = False
    set_time: str | None = None
    set_weather: str | None = None


@dataclass(frozen=True, slots=True)
class ResetCommandPlan:
    """A command plan derived from a task reset policy and worker identity."""

    commands: tuple[str, ...]
    reason: str

    def to_json(self) -> dict[str, Any]:
        """Convert the command plan into a JSON-safe audit payload."""

        return {"commands": list(self.commands), "reason": self.reason}


class NoopServerCommandExecutor:
    """Server command executor used when privileged reset is disabled."""

    async def execute_many(self, commands: Sequence[str]) -> list[ServerCommandResult]:
        """Return skipped command results without touching a server."""

        return [
            ServerCommandResult(
                command=command,
                ok=False,
                error="server_command_executor_disabled",
            )
            for command in commands
        ]


class RconProtocolError(RuntimeError):
    """Raised when a Minecraft RCON server sends an invalid response."""


@dataclass(frozen=True, slots=True)
class RconPacket:
    """One Minecraft RCON packet using the Source RCON wire format."""

    request_id: int
    packet_type: int
    payload: str


class MinecraftRconClient:
    """Small async Minecraft RCON client used for harness-owned reset commands."""

    AUTH_PACKET_TYPE = 3
    COMMAND_PACKET_TYPE = 2

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password: str,
        timeout_sec: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout_sec = timeout_sec
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_request_id = 1

    async def __aenter__(self) -> MinecraftRconClient:
        """Open and authenticate the RCON connection."""

        await self.connect()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Close the RCON connection."""

        await self.close()

    async def connect(self) -> None:
        """Connect to the RCON socket and authenticate with the configured password."""

        if self._reader is not None and self._writer is not None:
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout_sec,
        )
        response = await self._round_trip(self.AUTH_PACKET_TYPE, self.password)
        if response.request_id == -1:
            raise RconProtocolError("Minecraft RCON authentication failed.")

    async def command(self, command: str) -> str:
        """Execute one Minecraft command through the authenticated RCON connection."""

        response = await self._round_trip(self.COMMAND_PACKET_TYPE, command)
        return response.payload

    async def close(self) -> None:
        """Close the socket if it is open."""

        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is None:
            return
        writer.close()
        await writer.wait_closed()

    async def _round_trip(self, packet_type: int, payload: str) -> RconPacket:
        """Send one RCON packet and wait for its response."""

        request_id = self._next_request_id
        self._next_request_id += 1
        await self._send_packet(RconPacket(request_id, packet_type, payload))
        response = await self._read_packet()
        if response.request_id not in {request_id, -1}:
            raise RconProtocolError(
                f"Unexpected RCON response id {response.request_id}; expected {request_id}."
            )
        return response

    async def _send_packet(self, packet: RconPacket) -> None:
        """Write one encoded RCON packet to the socket."""

        if self._writer is None:
            raise RconProtocolError("RCON client is not connected.")
        payload = packet.payload.encode("utf-8")
        body = struct.pack("<ii", packet.request_id, packet.packet_type) + payload + b"\x00\x00"
        self._writer.write(struct.pack("<i", len(body)) + body)
        await asyncio.wait_for(self._writer.drain(), timeout=self.timeout_sec)

    async def _read_packet(self) -> RconPacket:
        """Read and decode one RCON packet from the socket."""

        if self._reader is None:
            raise RconProtocolError("RCON client is not connected.")
        header = await asyncio.wait_for(self._reader.readexactly(4), timeout=self.timeout_sec)
        (length,) = struct.unpack("<i", header)
        if length < 10:
            raise RconProtocolError(f"Invalid RCON packet length: {length}.")
        body = await asyncio.wait_for(self._reader.readexactly(length), timeout=self.timeout_sec)
        request_id, packet_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return RconPacket(request_id=request_id, packet_type=packet_type, payload=payload)


class RconServerCommandExecutor:
    """Server command executor backed by Minecraft RCON."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        password: str,
        timeout_sec: float = 3.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout_sec = timeout_sec
        self._batch_lock = asyncio.Lock()

    async def execute_many(self, commands: Sequence[str]) -> list[ServerCommandResult]:
        """Serialize command batches because the Minecraft RCON server is single-client."""

        if not commands:
            return []
        async with self._batch_lock:
            return await self._execute_many_locked(commands)

    async def _execute_many_locked(
        self,
        commands: Sequence[str],
    ) -> list[ServerCommandResult]:
        """Execute one ordered command batch over one authenticated RCON connection."""

        results: list[ServerCommandResult] = []
        try:
            async with MinecraftRconClient(
                host=self.host,
                port=self.port,
                password=self.password,
                timeout_sec=self.timeout_sec,
            ) as client:
                for command in commands:
                    started = asyncio.get_running_loop().time()
                    try:
                        response = await client.command(_strip_leading_slash(command))
                    except Exception as exc:  # noqa: BLE001 - each command result must be auditable.
                        results.append(
                            ServerCommandResult(
                                command=command,
                                ok=False,
                                error=f"{type(exc).__name__}: {exc}",
                                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                            )
                        )
                    else:
                        ok = _command_response_ok(command, response)
                        results.append(
                            ServerCommandResult(
                                command=command,
                                ok=ok,
                                response=response,
                                error=None if ok else response,
                                duration_ms=(asyncio.get_running_loop().time() - started) * 1000,
                            )
                        )
        except Exception as exc:  # noqa: BLE001 - connection/auth failures are returned as command results.
            return [
                ServerCommandResult(
                    command=command,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
                for command in commands
            ]
        return results


class ServerCommandResetRuntime:
    """Runtime wrapper that applies privileged server reset commands after worker reset."""

    def __init__(
        self,
        runtime: GameRuntime,
        executor: ServerCommandExecutor,
        config: ServerCommandResetConfig,
        biome_location_cache: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.executor = executor
        self.config = config
        self.biome_location_cache = biome_location_cache if biome_location_cache is not None else {}

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any] | None:
        """Reset the worker, then apply server-authorized reset commands."""

        worker_reset = await self.runtime.reset(task_spec)
        server_reset = await self._apply_server_reset(task_spec)
        if not isinstance(worker_reset, dict):
            return {"worker_reset": worker_reset, "server_command_reset": server_reset}
        return {
            **worker_reset,
            "worker_reset": worker_reset,
            "server_command_reset": server_reset,
            "reset_policy": {
                "worker": worker_reset.get("reset_policy"),
                "server_commands": server_reset,
            },
        }

    async def observe(self) -> dict[str, Any]:
        """Return the wrapped runtime observation."""

        return await self.runtime.observe()

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Execute one action in the wrapped runtime."""

        return await self.runtime.act(action)

    async def snapshot(self) -> dict[str, Any]:
        """Capture a snapshot from the wrapped runtime."""

        return await self.runtime.snapshot()

    async def close(self) -> None:
        """Close the wrapped runtime."""

        await self.runtime.close()

    async def _apply_server_reset(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Build and execute the server command reset plan."""

        plan = build_reset_command_plan(task_spec, self.config)
        if not self.config.enabled:
            return {
                "enabled": False,
                "plan": plan.to_json(),
                "results": [],
                "success": True,
            }
        biome_alignment = await self._align_task_biome(task_spec)
        plan_results = await self.executor.execute_many(plan.commands)
        results = [*biome_alignment["results"], *plan_results]
        success = bool(biome_alignment["success"]) and all(result.ok for result in plan_results)
        if self.config.enabled and not success:
            failures = [result.to_json() for result in results if not result.ok]
            raise RuntimeError(f"Server command reset failed: {failures}")
        return {
            "enabled": True,
            "plan": plan.to_json(),
            "results": [result.to_json() for result in results],
            "biome_alignment": {
                **biome_alignment,
                "results": [result.to_json() for result in biome_alignment["results"]],
            },
            "success": success,
        }

    async def _align_task_biome(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Locate and surface-teleport the worker into a specified task biome."""

        reset_plan = task_spec.get("reset_plan") if isinstance(task_spec.get("reset_plan"), dict) else {}
        biome = reset_plan.get("biome_hint")
        random_teleport = reset_plan.get("random_teleport")
        if not self.config.align_biome:
            return _skipped_biome_alignment("disabled")
        if not isinstance(biome, str) or not biome:
            return _skipped_biome_alignment("missing_biome_hint")
        if isinstance(random_teleport, dict) and random_teleport.get("enabled"):
            return _skipped_biome_alignment("random_teleport_override")
        if isinstance(reset_plan.get("start_position"), dict):
            return _skipped_biome_alignment("start_position_override")
        runtime = task_spec.get("runtime") if isinstance(task_spec.get("runtime"), dict) else {}
        username = runtime.get("username")
        if not isinstance(username, str) or not username:
            return _skipped_biome_alignment("missing_runtime_username")

        locate_command = f"/locate biome {_minecraft_id(biome)}"
        locate_results: list[ServerCommandResult] = []
        cached = self.biome_location_cache.get(biome)
        if cached is None:
            locate_results = await self.executor.execute_many([locate_command])
            locate_result = locate_results[0] if locate_results else None
            if locate_result is None or not locate_result.ok:
                return {
                    "enabled": True,
                    "success": False,
                    "biome": biome,
                    "cache_hit": False,
                    "reason": "locate_failed",
                    "position": None,
                    "results": locate_results,
                }
            cached = _parse_locate_biome_position(locate_result.response)
            if cached is None:
                parse_failure = ServerCommandResult(
                    command=locate_command,
                    ok=False,
                    response=locate_result.response,
                    error="unable_to_parse_locate_biome_response",
                )
                return {
                    "enabled": True,
                    "success": False,
                    "biome": biome,
                    "cache_hit": False,
                    "reason": "locate_response_unparseable",
                    "position": None,
                    "results": [*locate_results, parse_failure],
                }
            self.biome_location_cache[biome] = cached

        x, z = cached
        teleport_command = f"/spreadplayers {x} {z} 0 8 false {username}"
        teleport_results = await self.executor.execute_many([teleport_command])
        teleport_success = bool(teleport_results) and all(result.ok for result in teleport_results)
        return {
            "enabled": True,
            "success": teleport_success,
            "biome": biome,
            "cache_hit": not locate_results,
            "reason": "aligned" if teleport_success else "teleport_failed",
            "position": {"x": x, "z": z},
            "results": [*locate_results, *teleport_results],
        }


def build_reset_command_plan(
    task_spec: dict[str, Any],
    config: ServerCommandResetConfig,
) -> ResetCommandPlan:
    """Build server-side reset commands from a live task spec."""

    runtime = task_spec.get("runtime") if isinstance(task_spec.get("runtime"), dict) else {}
    username = runtime.get("username")
    if not isinstance(username, str) or not username:
        return ResetCommandPlan(commands=(), reason="missing_runtime_username")

    reset_policy = runtime.get("reset_policy") if isinstance(runtime.get("reset_policy"), dict) else {}
    clear_policy = (
        reset_policy.get("clear_inventory")
        if isinstance(reset_policy.get("clear_inventory"), dict)
        else {}
    )
    reset_plan = task_spec.get("reset_plan") if isinstance(task_spec.get("reset_plan"), dict) else {}
    owner_tag = _task_mob_owner_tag(username)
    task_tag = _task_mob_task_tag(str(task_spec.get("task_id") or "unknown_task"))
    commands: list[str] = []
    should_clear_inventory = bool(clear_policy.get("enabled")) or bool(reset_plan.get("clear_inventory"))
    if config.clear_inventory and should_clear_inventory:
        mode = str(clear_policy.get("mode") or "items")
        items = clear_policy.get("items") if isinstance(clear_policy.get("items"), list) else []
        if mode == "all" or reset_plan.get("clear_inventory"):
            commands.append(f"/clear {username}")
        else:
            commands.extend(
                f"/clear {username} {_minecraft_id(str(item))}"
                for item in items
                if isinstance(item, str) and item
            )
    if config.clear_dropped_items and reset_plan.get("clear_dropped_items", True):
        commands.append("/kill @e[type=item]")
    if config.apply_task_reset_plan:
        commands.append(f"/kill @e[tag={owner_tag}]")
    task_set_time = reset_plan.get("set_time") if config.apply_task_reset_plan else None
    task_set_weather = reset_plan.get("set_weather") if config.apply_task_reset_plan else None
    set_time = config.set_time if config.set_time is not None else task_set_time
    set_weather = config.set_weather if config.set_weather is not None else task_set_weather
    if set_time:
        commands.append(f"/time set {set_time}")
    if set_weather:
        commands.append(f"/weather {set_weather}")
    if config.restore_player_state:
        commands.extend(_restore_player_state_commands(username))
    if config.apply_task_reset_plan:
        commands.extend(
            _task_reset_commands(
                username,
                reset_plan,
                owner_tag=owner_tag,
                task_tag=task_tag,
            )
        )
    return ResetCommandPlan(commands=tuple(commands), reason="live_training_reset")


def _restore_player_state_commands(username: str) -> list[str]:
    """Build commands that reset persistent health and hunger between tasks."""

    return [
        f"/effect give {username} minecraft:instant_health 1 10 true",
        f"/effect give {username} minecraft:saturation 1 10 true",
    ]


def _task_reset_commands(
    username: str,
    reset_plan: dict[str, Any],
    *,
    owner_tag: str,
    task_tag: str,
) -> list[str]:
    """Build task-specific MineDojo-style reset commands after generic cleanup."""

    commands: list[str] = []
    game_mode = reset_plan.get("game_mode")
    if game_mode is not None:
        normalized_game_mode = str(game_mode).strip().casefold()
        if normalized_game_mode not in {"survival", "creative", "adventure", "spectator"}:
            raise ValueError(f"Unsupported reset game_mode: {game_mode!r}.")
        commands.append(f"/gamemode {normalized_game_mode} {username}")
    random_teleport = reset_plan.get("random_teleport")
    if isinstance(random_teleport, dict) and random_teleport.get("enabled"):
        center = random_teleport.get("center") if isinstance(random_teleport.get("center"), dict) else {}
        center_x = int(center.get("x", 0))
        center_z = int(center.get("z", 0))
        spread_distance = int(random_teleport.get("spread_distance", 0))
        max_range = int(random_teleport.get("max_range", 200))
        commands.append(f"/spreadplayers {center_x} {center_z} {spread_distance} {max_range} false {username}")

    start_position = reset_plan.get("start_position")
    if isinstance(start_position, dict):
        x = _number(start_position.get("x"))
        y = _number(start_position.get("y"))
        z = _number(start_position.get("z"))
        yaw = _number(start_position.get("yaw"))
        pitch = _number(start_position.get("pitch"))
        if x is not None and y is not None and z is not None:
            if yaw is not None and pitch is not None:
                commands.append(f"/tp {username} {x} {y} {z} {yaw} {pitch}")
            else:
                commands.append(f"/tp {username} {x} {y} {z}")

    initial_inventory = reset_plan.get("initial_inventory")
    if isinstance(initial_inventory, list):
        commands.extend(_initial_inventory_commands(username, initial_inventory))

    set_blocks = reset_plan.get("set_blocks")
    if isinstance(set_blocks, list):
        commands.extend(_set_block_commands(username, set_blocks))

    spawn_mobs = reset_plan.get("spawn_mobs")
    if isinstance(spawn_mobs, list):
        commands.extend(
            _spawn_mob_commands(
                username,
                spawn_mobs,
                owner_tag=owner_tag,
                task_tag=task_tag,
            )
        )
    return commands


def _initial_inventory_commands(username: str, items: list[Any]) -> list[str]:
    """Build Minecraft 1.20 item replacement commands for initial inventory entries."""

    commands: list[str] = []
    fallback_hotbar_index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item") or item.get("name")
        if not isinstance(item_id, str) or not item_id:
            continue
        slot = _command_slot(str(item.get("slot") or f"hotbar.{fallback_hotbar_index}"))
        count = max(1, int(item.get("count") or item.get("quantity") or 1))
        commands.append(f"/item replace entity {username} {slot} with {_minecraft_id(item_id)} {count}")
        fallback_hotbar_index += 1
    return commands


def _set_block_commands(username: str, blocks: list[Any]) -> list[str]:
    """Build relative setblock commands executed at the bot position."""

    commands: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = block.get("block") or block.get("name")
        position = block.get("relative_position") or block.get("position")
        if not isinstance(block_id, str) or not isinstance(position, dict):
            continue
        x = _relative_coordinate(position.get("x"))
        y = _relative_coordinate(position.get("y"))
        z = _relative_coordinate(position.get("z"))
        commands.append(f"/execute at {username} run setblock {x} {y} {z} {_minecraft_id(block_id)}")
    return commands


def _spawn_mob_commands(
    username: str,
    mobs: list[Any],
    *,
    owner_tag: str,
    task_tag: str,
) -> list[str]:
    """Build relative summon commands with worker-scoped cleanup tags."""

    commands: list[str] = []
    for mob in mobs:
        if not isinstance(mob, dict):
            continue
        entity = mob.get("entity") or mob.get("name")
        if not isinstance(entity, str) or not entity:
            continue
        count = max(1, int(mob.get("count") or 1))
        offset = _spawn_offset(mob)
        summon_nbt = _summon_nbt_with_task_tags(
            mob.get("summon_nbt"),
            owner_tag=owner_tag,
            task_tag=task_tag,
        )
        for _index in range(count):
            commands.append(
                f"/execute at {username} run summon {_minecraft_id(entity)} "
                f"{_relative_coordinate(offset[0])} {_relative_coordinate(offset[1])} {_relative_coordinate(offset[2])} "
                f"{summon_nbt}"
            )
    return commands


def _summon_nbt_with_task_tags(
    value: Any,
    *,
    owner_tag: str,
    task_tag: str,
) -> str:
    """Merge trusted task-specific summon NBT with mandatory cleanup tags."""

    tags = f'Tags:["{TASK_MOB_GLOBAL_TAG}","{owner_tag}","{task_tag}"]'
    if value is None:
        return f"{{{tags}}}"
    if not isinstance(value, str):
        raise ValueError("spawn_mobs[].summon_nbt must be an SNBT compound string.")
    text = value.strip()
    if "\n" in text or "\r" in text or not text.startswith("{") or not text.endswith("}"):
        raise ValueError("spawn_mobs[].summon_nbt must be one single-line SNBT compound.")
    body = text[1:-1].strip()
    if re.search(r"(?:^|,)\s*Tags\s*:", body, flags=re.IGNORECASE):
        raise ValueError("spawn_mobs[].summon_nbt must not override Harness cleanup tags.")
    return f"{{{body},{tags}}}" if body else f"{{{tags}}}"


def _spawn_offset(mob: dict[str, Any]) -> tuple[int, int, int]:
    """Choose a deterministic midpoint from MineDojo spawn range metadata."""

    low = mob.get("range_low") if isinstance(mob.get("range_low"), list) else [-4, 0, -4]
    high = mob.get("range_high") if isinstance(mob.get("range_high"), list) else [4, 0, 4]
    values = []
    for index in range(3):
        left = int(low[index]) if index < len(low) and isinstance(low[index], (int, float)) else 0
        right = int(high[index]) if index < len(high) and isinstance(high[index], (int, float)) else left
        midpoint = int((left + right) / 2)
        if midpoint == 0 and index in {0, 2}:
            midpoint = 2
        values.append(midpoint)
    return values[0], values[1], values[2]


def _command_slot(slot: str) -> str:
    """Map harness reset slots to Minecraft 1.20 `/item replace entity` slots."""

    return {
        "mainhand": "hotbar.0",
        "main_hand": "hotbar.0",
        "offhand": "weapon.offhand",
        "head": "armor.head",
        "chest": "armor.chest",
        "legs": "armor.legs",
        "feet": "armor.feet",
    }.get(slot, slot)


def _relative_coordinate(value: Any) -> str:
    """Render a relative coordinate for commands executed at the bot position."""

    number = _number(value)
    if number is None:
        return "~"
    if number == 0:
        return "~"
    return f"~{number:g}" if number > 0 else f"~-{abs(number):g}"


def _number(value: Any) -> float | None:
    """Return a numeric value or None for absent/invalid command coordinates."""

    return float(value) if isinstance(value, (int, float)) else None


def _command_response_ok(command: str, response: str) -> bool:
    """Return whether a Minecraft command response represents a successful reset command."""

    normalized = response.lower()
    hard_failures = (
        "unknown item",
        "unknown entity",
        "unknown or incomplete command",
        "incorrect argument",
        "expected whitespace",
        "no player was found",
        "unable to summon",
        "expected entity",
        "could not find",
        "must be in spectator mode",
        "cannot spectate",
        "can't spectate",
        "cannot spectate themselves",
    )
    if any(phrase in normalized for phrase in hard_failures):
        return False
    idempotent_entity_cleanup = (
        "@e[type=item]" in command
        or f"@e[tag={TASK_MOB_OWNER_TAG_PREFIX}" in command
    )
    if "no entity was found" in normalized and not idempotent_entity_cleanup:
        return False
    return True


def _skipped_biome_alignment(reason: str) -> dict[str, Any]:
    """Return a stable audit payload when biome alignment does not apply."""

    return {
        "enabled": False,
        "success": True,
        "biome": None,
        "cache_hit": False,
        "reason": reason,
        "position": None,
        "results": [],
    }


def _parse_locate_biome_position(response: str) -> tuple[int, int] | None:
    """Extract X/Z coordinates from a Minecraft `/locate biome` response."""

    match = re.search(
        r"\[\s*(-?\d+)\s*,\s*(?:~|-?\d+)\s*,\s*(-?\d+)\s*\]",
        response,
    )
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _task_mob_owner_tag(username: str) -> str:
    """Return a selector-safe tag that isolates task mobs by worker username."""

    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", username).strip("_").lower() or "worker"
    return f"{TASK_MOB_OWNER_TAG_PREFIX}{normalized}"[:64]


def _task_mob_task_tag(task_id: str) -> str:
    """Return a bounded readable task tag with a collision-resistant suffix."""

    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", task_id).strip("_").lower() or "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return f"mc_agent_task_{normalized[:32]}_{digest}"


def _minecraft_id(item: str) -> str:
    """Return a namespaced Minecraft id for command arguments."""

    return item if ":" in item else f"minecraft:{item}"


def _strip_leading_slash(command: str) -> str:
    """RCON accepts commands without a leading slash."""

    return command[1:] if command.startswith("/") else command
