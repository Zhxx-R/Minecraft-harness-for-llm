import asyncio

import pytest

import mc_agent_harness.runtime.server_commands as server_commands
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.runtime.server_commands import (
    RconServerCommandExecutor,
    ServerCommandResetConfig,
    ServerCommandResetRuntime,
    ServerCommandResult,
    _command_response_ok,
    _parse_locate_biome_position,
    _task_mob_task_tag,
    build_reset_command_plan,
)
from mc_agent_harness.schemas.action import HarnessAction


def test_build_reset_command_plan_uses_task_reset_policy() -> None:
    """Server reset plans should mirror live task reset policy without exposing credentials."""

    plan = build_reset_command_plan(
        {
            "runtime": {
                "username": "Trainer1",
                "reset_policy": {
                    "clear_inventory": {
                        "enabled": True,
                        "mode": "items",
                        "items": ["oak_log"],
                    }
                },
            }
        },
        ServerCommandResetConfig(enabled=True),
    )

    assert plan.commands == (
        "/clear Trainer1 minecraft:oak_log",
        "/kill @e[type=item]",
        "/kill @e[tag=mc_agent_owner_trainer1]",
        "/effect give Trainer1 minecraft:instant_health 1 10 true",
        "/effect give Trainer1 minecraft:saturation 1 10 true",
    )


def test_build_reset_command_plan_uses_minedojo_reset_plan() -> None:
    """Server reset plans should execute MineDojo-style inventory, spawn, and time setup."""

    plan = build_reset_command_plan(
        {
            "runtime": {"username": "Trainer1", "reset_policy": {"clear_inventory": {"enabled": False}}},
            "reset_plan": {
                "game_mode": "survival",
                "clear_inventory": True,
                "clear_dropped_items": True,
                "set_time": "night",
                "set_weather": "clear",
                "initial_inventory": [
                    {"slot": "mainhand", "item": "diamond_sword", "count": 1},
                    {"slot": "offhand", "item": "shield", "count": 1},
                ],
                "spawn_mobs": [
                    {"entity": "zombie", "count": 1, "range_low": [-7, 1, -7], "range_high": [7, 1, 7]}
                ],
            },
        },
        ServerCommandResetConfig(enabled=True),
    )

    assert plan.commands == (
        "/clear Trainer1",
        "/kill @e[type=item]",
        "/kill @e[tag=mc_agent_owner_trainer1]",
        "/time set night",
        "/weather clear",
        "/effect give Trainer1 minecraft:instant_health 1 10 true",
        "/effect give Trainer1 minecraft:saturation 1 10 true",
        "/gamemode survival Trainer1",
        "/item replace entity Trainer1 hotbar.0 with minecraft:diamond_sword 1",
        "/item replace entity Trainer1 weapon.offhand with minecraft:shield 1",
        (
            '/execute at Trainer1 run summon minecraft:zombie ~2 ~1 ~2 '
            f'{{Tags:["mc_agent_task_mob","mc_agent_owner_trainer1","{_task_mob_task_tag("unknown_task")}"]}}'
        ),
    )


def test_build_reset_command_plan_supports_random_teleport() -> None:
    """Server reset plans should translate random reset teleport into spreadplayers."""

    plan = build_reset_command_plan(
        {
            "runtime": {"username": "Trainer1", "reset_policy": {"clear_inventory": {"enabled": False}}},
            "reset_plan": {
                "clear_dropped_items": False,
                "random_teleport": {
                    "enabled": True,
                    "center": {"x": 100, "z": -50},
                    "spread_distance": 4,
                    "max_range": 300,
                },
            },
        },
        ServerCommandResetConfig(enabled=True, restore_player_state=False),
    )

    assert plan.commands == (
        "/kill @e[tag=mc_agent_owner_trainer1]",
        "/spreadplayers 100 -50 4 300 false Trainer1",
    )


def test_build_reset_command_plan_merges_spawn_nbt_with_cleanup_tags() -> None:
    """Demo fixtures may configure entity metadata without losing scoped cleanup."""

    plan = build_reset_command_plan(
        {
            "runtime": {"username": "Trainer1"},
            "reset_plan": {
                "clear_dropped_items": False,
                "spawn_mobs": [
                    {
                        "entity": "sheep",
                        "count": 1,
                        "range_low": [2, 0, 2],
                        "range_high": [2, 0, 2],
                        "summon_nbt": "{Color:12b}",
                    }
                ],
            },
        },
        ServerCommandResetConfig(enabled=True, restore_player_state=False),
    )

    assert plan.commands[1] == (
        "/execute at Trainer1 run summon minecraft:sheep ~2 ~ ~2 "
        f'{{Color:12b,Tags:["mc_agent_task_mob","mc_agent_owner_trainer1",'
        f'"{_task_mob_task_tag("unknown_task")}"]}}'
    )


def test_build_reset_command_plan_isolates_spawned_mobs_by_worker() -> None:
    """Concurrent workers should clean only their own previously spawned task mobs."""

    first = build_reset_command_plan(
        {
            "runtime": {"username": "Trainer_One"},
            "reset_plan": {"clear_dropped_items": False, "spawn_mobs": [{"entity": "pig", "count": 1}]},
        },
        ServerCommandResetConfig(enabled=True, restore_player_state=False),
    )
    second = build_reset_command_plan(
        {
            "runtime": {"username": "Trainer_Two"},
            "reset_plan": {"clear_dropped_items": False, "spawn_mobs": [{"entity": "pig", "count": 1}]},
        },
        ServerCommandResetConfig(enabled=True, restore_player_state=False),
    )

    assert first.commands[0] == "/kill @e[tag=mc_agent_owner_trainer_one]"
    assert second.commands[0] == "/kill @e[tag=mc_agent_owner_trainer_two]"
    assert "mc_agent_owner_trainer_one" in first.commands[1]
    assert "mc_agent_owner_trainer_two" in second.commands[1]
    assert "mc_agent_owner_trainer_two" not in first.commands[1]
    assert _task_mob_task_tag("unknown_task") in first.commands[1]


@pytest.mark.anyio
async def test_server_command_reset_runtime_merges_worker_and_server_audit() -> None:
    """The runtime wrapper should return worker reset and server-command reset metadata together."""

    executor = FakeServerCommandExecutor()
    runtime = ServerCommandResetRuntime(
        FakeRuntime(),
        executor=executor,
        config=ServerCommandResetConfig(enabled=True),
    )

    result = await runtime.reset(
        {
            "runtime": {
                "username": "Trainer1",
                "reset_policy": {
                    "clear_inventory": {
                        "enabled": True,
                        "mode": "all",
                        "items": [],
                    }
                },
            }
        }
    )

    assert result is not None
    assert result["worker_reset"]["ok"] is True
    assert result["server_command_reset"]["success"] is True
    assert executor.commands == [
        "/clear Trainer1",
        "/kill @e[type=item]",
        "/kill @e[tag=mc_agent_owner_trainer1]",
        "/effect give Trainer1 minecraft:instant_health 1 10 true",
        "/effect give Trainer1 minecraft:saturation 1 10 true",
    ]
    assert result["reset_policy"]["server_commands"]["results"][0]["command"] == "/clear Trainer1"


@pytest.mark.anyio
async def test_server_command_reset_runtime_raises_on_failed_server_command() -> None:
    """A bad reset command should stop the run instead of training on a mismatched environment."""

    runtime = ServerCommandResetRuntime(
        FakeRuntime(),
        executor=FailingServerCommandExecutor(),
        config=ServerCommandResetConfig(enabled=True),
    )

    with pytest.raises(RuntimeError, match="Server command reset failed"):
        await runtime.reset(
            {
                "runtime": {"username": "Trainer1", "reset_policy": {"clear_inventory": {"enabled": False}}},
                "reset_plan": {
                    "clear_inventory": False,
                    "clear_dropped_items": False,
                    "initial_inventory": [{"slot": "feet", "item": "hills_diamond_boots", "count": 1}],
                },
            }
        )


@pytest.mark.anyio
async def test_server_command_reset_aligns_and_caches_task_biome() -> None:
    """Biome hints should locate once and reuse coordinates on later server resets."""

    executor = BiomeServerCommandExecutor()
    runtime = ServerCommandResetRuntime(
        FakeRuntime(),
        executor=executor,
        config=ServerCommandResetConfig(
            enabled=True,
            align_biome=True,
            clear_dropped_items=False,
            restore_player_state=False,
        ),
    )
    task_spec = {
        "runtime": {
            "username": "Trainer1",
            "reset_policy": {"clear_inventory": {"enabled": False}},
        },
        "reset_plan": {
            "biome_hint": "windswept_hills",
            "clear_dropped_items": False,
        },
    }

    first = await runtime.reset(task_spec)
    second = await runtime.reset(task_spec)

    assert first is not None and second is not None
    first_alignment = first["server_command_reset"]["biome_alignment"]
    second_alignment = second["server_command_reset"]["biome_alignment"]
    assert first_alignment["position"] == {"x": 120, "z": -340}
    assert first_alignment["cache_hit"] is False
    assert second_alignment["cache_hit"] is True
    assert executor.commands.count("/locate biome minecraft:windswept_hills") == 1
    assert executor.commands.count("/spreadplayers 120 -340 0 8 false Trainer1") == 2


def test_parse_locate_biome_position_supports_tilde_height() -> None:
    """The parser should retain X/Z from the standard 1.20 locate response."""

    assert _parse_locate_biome_position(
        "The nearest minecraft:plains is at [123, ~, -456] (579 blocks away)"
    ) == (123, -456)


def test_command_response_ok_rejects_minecraft_command_errors() -> None:
    """RCON textual command errors should be audited as failed reset commands."""

    assert _command_response_ok(
        "/item replace entity Trainer1 armor.feet with minecraft:hills_diamond_boots 1",
        "Unknown item 'minecraft:hills_diamond_boots'",
    ) is False
    assert _command_response_ok("/kill @e[type=item]", "No entity was found") is True
    assert _command_response_ok(
        "/kill @e[tag=mc_agent_owner_trainer1]",
        "No entity was found",
    ) is True
    assert _command_response_ok("/kill @e[type=pig]", "No entity was found") is False
    assert _command_response_ok("/item replace entity Trainer1 hotbar.0 with minecraft:iron_sword 1", "Replaced a slot") is True


@pytest.mark.anyio
async def test_rcon_executor_serializes_concurrent_command_batches(monkeypatch) -> None:
    """One shared executor should not overlap RCON sessions across worker resets."""

    active_clients = 0
    max_active_clients = 0

    class FakeRconClient:
        """Instrumented RCON client that records overlapping active sessions."""

        def __init__(self, **_kwargs) -> None:
            self.connected = False

        async def __aenter__(self):
            nonlocal active_clients, max_active_clients
            active_clients += 1
            max_active_clients = max(max_active_clients, active_clients)
            self.connected = True
            return self

        async def __aexit__(self, *_exc_info) -> None:
            nonlocal active_clients
            active_clients -= 1
            self.connected = False

        async def command(self, command: str) -> str:
            assert self.connected is True
            await asyncio.sleep(0.01)
            return f"ok:{command}"

    monkeypatch.setattr(server_commands, "MinecraftRconClient", FakeRconClient)
    executor = RconServerCommandExecutor(
        host="localhost",
        port=25575,
        password="test-password",
    )

    first, second = await asyncio.gather(
        executor.execute_many(["/say first"]),
        executor.execute_many(["/say second"]),
    )

    assert max_active_clients == 1
    assert first[0].response == "ok:say first"
    assert second[0].response == "ok:say second"


class FakeServerCommandExecutor:
    """In-memory server command executor for reset wrapper tests."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute_many(self, commands) -> list[ServerCommandResult]:
        """Record commands and mark them successful."""

        self.commands.extend(commands)
        return [ServerCommandResult(command=command, ok=True, response="ok") for command in commands]


class FailingServerCommandExecutor:
    """Server command executor that simulates a Minecraft command error."""

    async def execute_many(self, commands) -> list[ServerCommandResult]:
        """Mark the first command as a failed server reset command."""

        return [
            ServerCommandResult(
                command=command,
                ok=False,
                response="Unknown item 'minecraft:hills_diamond_boots'",
                error="Unknown item 'minecraft:hills_diamond_boots'",
            )
            for command in commands
        ]


class BiomeServerCommandExecutor:
    """Command executor that emulates locate-biome and teleport responses."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def execute_many(self, commands) -> list[ServerCommandResult]:
        """Return a parseable locate response and acknowledge other commands."""

        self.commands.extend(commands)
        return [
            ServerCommandResult(
                command=command,
                ok=True,
                response=(
                    "The nearest minecraft:windswept_hills is at [120, ~, -340] "
                    "(400 blocks away)"
                    if command.startswith("/locate biome")
                    else "ok"
                ),
            )
            for command in commands
        ]


class FakeRuntime(GameRuntime):
    """Minimal runtime that returns a worker reset policy for wrapper tests."""

    async def reset(self, task_spec: dict[str, object]) -> dict[str, object]:
        """Return fake worker reset metadata."""

        return {
            "ok": True,
            "reset_policy": {"worker": "reset"},
            "task_id": task_spec.get("task_id"),
        }

    async def observe(self) -> dict[str, object]:
        """Return an empty fake observation."""

        return {}

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Return a fake action result."""

        return {"ok": True, "action_type": action.type}

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""
