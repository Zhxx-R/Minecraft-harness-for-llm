from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "run_week10_live_training.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "run_week10_live_training_script",
    SCRIPT_PATH,
)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
LIVE_SCRIPT = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = LIVE_SCRIPT
SCRIPT_SPEC.loader.exec_module(LIVE_SCRIPT)


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


class FakeCommandResult:
    """Successful RCON command result used by spectator-control tests."""

    def __init__(
        self,
        command: str,
        *,
        ok: bool = True,
        response: str = "",
    ) -> None:
        self.command = command
        self.ok = ok
        self.response = response
        self.error = None if ok else response

    def to_json(self) -> dict[str, Any]:
        """Return the audit shape consumed by the live training script."""

        return {
            "command": self.command,
            "ok": self.ok,
            "response": self.response,
            "error": self.error,
        }


class FakeRconExecutor:
    """In-memory executor that records spectator and restore command batches."""

    def __init__(
        self,
        entity_positions: dict[str, tuple[float, float, float]] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.entity_positions = {
            "HarnessTrainer1": (0.0, 64.0, 0.0),
            "flysnow_chen": (0.0, 64.0, 0.0),
            **(entity_positions or {}),
        }

    def set_entity_position(
        self,
        entity: str,
        position: tuple[float, float, float],
    ) -> None:
        """Update the position returned by a later RCON entity-data query."""

        self.entity_positions[entity] = position

    async def execute_many(self, commands: list[str]) -> list[FakeCommandResult]:
        """Record and acknowledge one RCON command batch."""

        self.calls.append(list(commands))
        results: list[FakeCommandResult] = []
        for command in commands:
            position_target = _position_query_target(command)
            if position_target is not None:
                position = self.entity_positions.get(position_target)
                if position is None:
                    results.append(
                        FakeCommandResult(
                            command,
                            ok=False,
                            response="No entity was found",
                        )
                    )
                    continue
                x, y, z = position
                results.append(
                    FakeCommandResult(
                        command,
                        response=(
                            f"{position_target} has the following entity data: [{x}d, {y}d, {z}d]"
                        ),
                    )
                )
                continue
            _apply_fake_teleport(command, self.entity_positions)
            results.append(FakeCommandResult(command))
        return results


def _position_query_target(command: str) -> str | None:
    """Extract the target from `/data get entity <target> Pos`."""

    parts = command.removeprefix("/").split()
    for index in range(len(parts) - 3):
        if parts[index : index + 3] == ["data", "get", "entity"]:
            target = parts[index + 3]
            if index + 4 < len(parts) and parts[index + 4] == "Pos":
                return target
    return None


def _apply_fake_teleport(
    command: str,
    positions: dict[str, tuple[float, float, float]],
) -> None:
    """Mirror an entity-to-entity teleport in the fake server state."""

    parts = command.removeprefix("/").split()
    if len(parts) != 3 or parts[0] not in {"tp", "teleport"}:
        return
    subject, target = parts[1], parts[2]
    if target in positions:
        positions[subject] = positions[target]


def _spectate_call_count(executor: FakeRconExecutor) -> int:
    """Count camera attachments without counting position probes."""

    return sum(command.startswith("/spectate ") for batch in executor.calls for command in batch)


async def _wait_for_spectate_calls(
    executor: FakeRconExecutor,
    expected: int,
    *,
    timeout_sec: float = 0.5,
) -> None:
    """Wait until the spectator loop has issued the expected attachment count."""

    async def wait() -> None:
        while _spectate_call_count(executor) < expected:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout=timeout_sec)


async def _publish_camera_state(
    event_stream: Any,
    *,
    run_id: str = "run-1",
    entity_id: int = 41,
    spawn_sequence: int = 1,
    position: tuple[float, float, float] = (0.0, 64.0, 0.0),
    dimension: str = "overworld",
) -> None:
    """Publish the compact observation fields consumed by spectator resynchronization."""

    await event_stream.publish(
        run_id,
        "observation",
        {
            "agent_id": "HarnessTrainer1",
            "worker_id": "worker-1",
            "observation": {
                "entity_id": entity_id,
                "spawn_sequence": spawn_sequence,
                "position": {
                    "x": position[0],
                    "y": position[1],
                    "z": position[2],
                },
                "world": {"dimension": dimension},
            },
        },
    )


class FakeTrainingRunner:
    """Small runner that yields control so the spectator task can execute."""

    def __init__(self, event_stream: Any) -> None:
        """Store the same committed-event stream subscribed by spectator control."""

        self.event_stream = event_stream

    async def run(self, task_ids: list[str]) -> dict[str, Any]:
        """Return a deterministic report after one event-loop turn."""

        await self.event_stream.publish(
            "run-1",
            "run_started",
            {
                "task_id": task_ids[0],
                "agent_id": "HarnessTrainer1",
                "worker_id": "worker-1",
            },
        )
        await asyncio.sleep(0.01)
        return {"task_ids": task_ids}


class DelayedFakeTrainingRunner:
    """Runner that remains active long enough to exercise spectator keepalive."""

    def __init__(self, event_stream: Any) -> None:
        """Store the event stream used to announce reset completion."""

        self.event_stream = event_stream

    async def run(self, task_ids: list[str]) -> dict[str, Any]:
        """Return after the configured minimum spectator rebind delay."""

        await self.event_stream.publish(
            "run-1",
            "run_started",
            {
                "task_id": task_ids[0],
                "agent_id": "HarnessTrainer1",
                "worker_id": "worker-1",
            },
        )
        await asyncio.sleep(0.15)
        return {"task_ids": task_ids}


@pytest.mark.anyio
async def test_spectator_follow_works_without_video_recording() -> None:
    """A spectator player should be controlled even when ffmpeg recording is disabled."""

    args = SimpleNamespace(
        record_agent_video=False,
        spectator_player="flysnow_chen",
        recording_spectate_retries=2,
        recording_spectate_interval_sec=0.01,
        spectator_chunk_sync_delay_sec=0.0,
        spectator_rebind_interval_sec=10.0,
        spectator_full_sync_interval_sec=0.0,
        spectator_resync_distance_blocks=32.0,
        spectator_resync_cooldown_sec=0.0,
        recording_no_restore_spectator=False,
        agent_visual_snapshots=True,
    )
    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    audited: list[tuple[str, str, dict[str, Any]]] = []

    async def audit(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Capture spectator events as the SQL-backed callback would."""

        audited.append((run_id, event_type, dict(payload)))

    worker = LIVE_SCRIPT.LiveWorkerSpec(
        worker_id="worker-1",
        worker_url="ws://127.0.0.1:8765",
        username="HarnessTrainer1",
    )
    visual_ready = asyncio.Event()

    report, session = await LIVE_SCRIPT._run_training_with_optional_recording(
        runner=FakeTrainingRunner(event_stream),
        task_ids=["harvest_1_dirt"],
        args=args,
        worker_specs=[worker],
        rcon_executor=executor,
        recording_output_path=None,
        run_event_stream=event_stream,
        spectator_audit_callback=audit,
        visual_readiness_event=visual_ready,
    )

    assert report == {"task_ids": ["harvest_1_dirt"]}
    assert session["enabled"] is False
    assert session["spectator_player"] == "flysnow_chen"
    assert session["spectated_username"] == "HarnessTrainer1"
    assert executor.calls[0] == [
        "/gamemode creative flysnow_chen",
        "/gamemode spectator flysnow_chen",
        "/tp flysnow_chen HarnessTrainer1",
    ]
    assert executor.calls[1] == ["/spectate HarnessTrainer1 flysnow_chen"]
    assert executor.calls[-1] == ["/gamemode creative flysnow_chen"]
    assert session["spectate_attempts"][0]["trigger"] == "post_reset_run_started"
    assert session["spectate_attempts"][0]["run_id"] == "run-1"
    assert audited[0][0:2] == ("run-1", "spectator_follow_attempt")
    assert audited[0][2]["success"] is True
    assert visual_ready.is_set() is True
    assert session["camera_ready_before_recording"] is True


@pytest.mark.anyio
async def test_stable_spectator_state_does_not_reissue_spectate() -> None:
    """Polling an unchanged camera state must not reset an attached client view."""

    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    await event_stream.publish(
        "run-1",
        "run_started",
        {
            "task_id": "creative:test",
            "agent_id": "HarnessTrainer1",
            "worker_id": "worker-1",
        },
    )
    await _publish_camera_state(event_stream)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        LIVE_SCRIPT._spectate_agent(
            executor=executor,
            spectator_player="flysnow_chen",
            target_username="HarnessTrainer1",
            retries=2,
            interval_sec=0.001,
            chunk_sync_delay_sec=0.0,
            rebind_interval_sec=0.01,
            full_sync_interval_sec=0.0,
            resync_distance_blocks=32.0,
            resync_cooldown_sec=0.0,
            stop_event=stop_event,
            run_event_stream=event_stream,
        )
    )

    await _wait_for_spectate_calls(executor, 1)
    await asyncio.sleep(0.06)
    stop_event.set()
    attempts = await task

    assert _spectate_call_count(executor) == 1
    assert len(attempts) == 1
    assert attempts[0]["phase"] == "post_reset_sync"


@pytest.mark.anyio
async def test_live_run_event_stream_parses_agent_camera_position() -> None:
    """Committed observations should expose numeric camera state without retaining the payload."""

    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")

    await _publish_camera_state(
        event_stream,
        entity_id=73,
        spawn_sequence=4,
        position=(12.25, 70.0, -8.5),
        dimension="the_nether",
    )

    state = event_stream.latest_camera_state("run-1")
    assert state is not None
    assert state.entity_id == 73
    assert state.spawn_sequence == 4
    assert state.position == (12.25, 70.0, -8.5)
    assert state.dimension == "the_nether"


def test_parse_entity_position_response_accepts_minecraft_nbt_vector() -> None:
    """RCON NBT suffixes should be converted into an ordinary numeric coordinate."""

    response = "flysnow_chen has the following entity data: [12.25d, 70.0d, -8.5d]"

    assert LIVE_SCRIPT._parse_entity_position_response(response) == (
        12.25,
        70.0,
        -8.5,
    )


@pytest.mark.anyio
async def test_spectator_distance_threshold_triggers_one_soft_sync() -> None:
    """Actual spectator drift beyond the threshold should teleport and attach exactly once."""

    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    await event_stream.publish(
        "run-1",
        "run_started",
        {
            "task_id": "creative:test",
            "agent_id": "HarnessTrainer1",
            "worker_id": "worker-1",
        },
    )
    await _publish_camera_state(event_stream)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        LIVE_SCRIPT._spectate_agent(
            executor=executor,
            spectator_player="flysnow_chen",
            target_username="HarnessTrainer1",
            retries=2,
            interval_sec=0.001,
            chunk_sync_delay_sec=0.0,
            rebind_interval_sec=0.01,
            full_sync_interval_sec=0.0,
            resync_distance_blocks=32.0,
            resync_cooldown_sec=0.0,
            stop_event=stop_event,
            run_event_stream=event_stream,
        )
    )
    await _wait_for_spectate_calls(executor, 1)

    moved_position = (96.0, 64.0, 0.0)
    executor.set_entity_position("HarnessTrainer1", moved_position)
    await _publish_camera_state(event_stream, position=moved_position)
    await _wait_for_spectate_calls(executor, 2)
    await asyncio.sleep(0.04)
    stop_event.set()
    attempts = await task

    assert _spectate_call_count(executor) == 2
    teleport_batches = [
        batch for batch in executor.calls if "/tp flysnow_chen HarnessTrainer1" in batch
    ]
    assert len(teleport_batches) == 2
    assert teleport_batches[1] == ["/tp flysnow_chen HarnessTrainer1"]
    assert len(attempts) == 2


@pytest.mark.anyio
async def test_spawn_sequence_change_triggers_one_soft_sync() -> None:
    """A respawn generation change should reattach even when identity and position are stable."""

    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    await event_stream.publish(
        "run-1",
        "run_started",
        {
            "task_id": "creative:test",
            "agent_id": "HarnessTrainer1",
            "worker_id": "worker-1",
        },
    )
    await _publish_camera_state(event_stream, spawn_sequence=1)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        LIVE_SCRIPT._spectate_agent(
            executor=executor,
            spectator_player="flysnow_chen",
            target_username="HarnessTrainer1",
            retries=2,
            interval_sec=0.001,
            chunk_sync_delay_sec=0.0,
            rebind_interval_sec=0.01,
            full_sync_interval_sec=0.0,
            resync_distance_blocks=32.0,
            resync_cooldown_sec=0.0,
            stop_event=stop_event,
            run_event_stream=event_stream,
        )
    )
    await _wait_for_spectate_calls(executor, 1)

    await _publish_camera_state(event_stream, spawn_sequence=2)
    await _wait_for_spectate_calls(executor, 2)
    await asyncio.sleep(0.04)
    stop_event.set()
    attempts = await task

    assert _spectate_call_count(executor) == 2
    teleport_batches = [
        batch for batch in executor.calls if "/tp flysnow_chen HarnessTrainer1" in batch
    ]
    assert len(teleport_batches) == 2
    assert teleport_batches[1] == ["/tp flysnow_chen HarnessTrainer1"]
    assert len(attempts) == 2


@pytest.mark.anyio
async def test_spectator_periodically_full_syncs_for_chunk_loading() -> None:
    """Long-distance demos can reload chunks before reattaching the camera."""

    args = SimpleNamespace(
        record_agent_video=False,
        spectator_player="flysnow_chen",
        recording_spectate_retries=2,
        recording_spectate_interval_sec=0.001,
        spectator_chunk_sync_delay_sec=0.0,
        spectator_rebind_interval_sec=0.001,
        spectator_full_sync_interval_sec=0.001,
        spectator_resync_distance_blocks=0.0,
        spectator_resync_cooldown_sec=0.0,
        recording_no_restore_spectator=False,
    )
    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    worker = LIVE_SCRIPT.LiveWorkerSpec(
        worker_id="worker-1",
        worker_url="ws://127.0.0.1:8765",
        username="HarnessTrainer1",
    )

    _, session = await LIVE_SCRIPT._run_training_with_optional_recording(
        runner=DelayedFakeTrainingRunner(event_stream),
        task_ids=["harvest_1_dirt"],
        args=args,
        worker_specs=[worker],
        rcon_executor=executor,
        recording_output_path=None,
        run_event_stream=event_stream,
    )

    periodic = [
        attempt
        for attempt in session["spectate_attempts"]
        if attempt["phase"] == "periodic_full_sync"
    ]
    assert periodic
    assert periodic[0]["trigger"] == "periodic_chunk_sync"
    assert periodic[0]["preparation_commands"]


@pytest.mark.anyio
async def test_spectator_waits_for_committed_run_start_before_rcon() -> None:
    """No camera command should run while the worker is still connecting or resetting."""

    executor = FakeRconExecutor()
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        LIVE_SCRIPT._spectate_agent(
            executor=executor,
            spectator_player="flysnow_chen",
            target_username="HarnessTrainer1",
            retries=2,
            interval_sec=0.01,
            chunk_sync_delay_sec=0.0,
            rebind_interval_sec=10.0,
            full_sync_interval_sec=0.0,
            resync_distance_blocks=32.0,
            resync_cooldown_sec=0.0,
            stop_event=stop_event,
            run_event_stream=event_stream,
        )
    )

    await asyncio.sleep(0.01)
    assert executor.calls == []
    await event_stream.publish(
        "run-after-reset",
        "run_started",
        {
            "task_id": "creative:1",
            "agent_id": "HarnessTrainer1",
            "worker_id": "worker-1",
        },
    )
    await asyncio.sleep(0.01)
    stop_event.set()
    attempts = await task

    assert executor.calls[0][-1] == "/tp flysnow_chen HarnessTrainer1"
    assert attempts[0]["success"] is True
    assert attempts[0]["run_id"] == "run-after-reset"


def test_live_cli_loads_two_isolated_server_pool_entries(tmp_path: Path) -> None:
    """The live CLI should consume distinct server placements from persisted pool state."""

    import json

    state_path = tmp_path / "server_pool_state.json"
    state_path.write_text(
        json.dumps(
            {
                "started_at": "2026-07-12T00:00:00Z",
                "servers": [
                    {
                        "server_id": "server-1",
                        "host": "127.0.0.1",
                        "server_port": 25565,
                        "rcon_port": 25575,
                        "world_dir": str(tmp_path / "server-1" / "world"),
                        "heap_gb": 2.5,
                        "max_workers": 1,
                    },
                    {
                        "server_id": "server-2",
                        "host": "127.0.0.1",
                        "server_port": 25566,
                        "rcon_port": 25576,
                        "world_dir": str(tmp_path / "server-2" / "world"),
                        "heap_gb": 2.5,
                        "max_workers": 1,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        server_pool_state=state_path,
        host="localhost",
        port=25565,
        rcon_port=25575,
        rcon_reset=True,
        threat_pause=False,
        spectator_player=None,
        rcon_random_teleport_on_reset=False,
    )

    servers, state = LIVE_SCRIPT._resolve_server_pool(args, 2)

    assert [server.server_id for server in servers] == ["server-1", "server-2"]
    assert [server.server_port for server in servers] == [25565, 25566]
    assert state is not None
    LIVE_SCRIPT._validate_parallel_placement(
        SimpleNamespace(allow_shared_server_workers=False),
        2,
        servers,
    )


def test_live_cli_rejects_two_workers_on_one_shared_server() -> None:
    """Formal parallel mode should fail before two workers share mutable world state."""

    server = LIVE_SCRIPT.MinecraftServerInstanceSpec(
        server_id="server-1",
        host="localhost",
        server_port=25565,
        rcon_port=25575,
        world_dir="shared-world",
        max_workers=2,
    )

    with pytest.raises(SystemExit, match="one isolated Minecraft server per worker"):
        LIVE_SCRIPT._validate_parallel_placement(
            SimpleNamespace(allow_shared_server_workers=False),
            2,
            [server],
        )


def test_live_cli_allocates_proportional_category_quotas() -> None:
    """A 100-task full batch should preserve the executable dataset category mix."""

    summaries = [
        *[{"category": "harvest"} for _ in range(895)],
        *[{"category": "combat"} for _ in range(471)],
        *[{"category": "techtree"} for _ in range(213)],
    ]

    quotas = LIVE_SCRIPT._proportional_category_quotas(summaries, 100)

    assert quotas == {"harvest": 57, "combat": 30, "techtree": 13}


def test_live_cli_allows_zero_quota_for_tiny_category() -> None:
    """A 100-task batch should not force one of only two survival tasks into the mix."""

    summaries = [
        *[{"category": "harvest"} for _ in range(895)],
        *[{"category": "combat"} for _ in range(471)],
        *[{"category": "techtree"} for _ in range(213)],
        *[{"category": "survival"} for _ in range(2)],
    ]

    quotas = LIVE_SCRIPT._proportional_category_quotas(summaries, 100)

    assert quotas == {"harvest": 57, "combat": 30, "techtree": 13, "survival": 0}


@pytest.mark.anyio
async def test_live_cli_skips_greedy_selection_for_complete_catalog() -> None:
    """Selecting every candidate should defer diversity work to the wave planner."""

    class CompleteCatalogProvider:
        async def list_tasks(self) -> list[dict[str, str]]:
            return [
                {"task_id": "harvest_1_dirt", "category": "harvest"},
                {"task_id": "combat_chicken", "category": "combat"},
                {"task_id": "survival_1", "category": "survival"},
            ]

        async def load_task(self, _task_id: str) -> dict[str, Any]:
            raise AssertionError("full-catalog selection must not load every task")

    args = SimpleNamespace(
        task_id=None,
        category=["harvest", "combat", "survival"],
        exclude_category=None,
        diverse_batch_size=3,
        stratified_batch=True,
        max_task_similarity=0.45,
    )

    selected = await LIVE_SCRIPT._select_task_ids(CompleteCatalogProvider(), args)

    assert selected == ["harvest_1_dirt", "combat_chicken", "survival_1"]


def test_live_cli_checkpoint_round_trip_restores_completed_attempt(tmp_path: Path) -> None:
    """Atomic checkpoint JSON should reconstruct one completed-wave resume state."""

    path = tmp_path / "live.checkpoint.json"
    outcome = LIVE_SCRIPT.LiveTrainingOutcome(
        task_id="harvest_1_dirt",
        attempt=1,
        run_id="run-1",
        worker_id="worker-1",
        username="Trainer1",
        server_id="server-1",
        memory_namespace="job:harvest_1_dirt:attempt-1",
        success=False,
        status="failed",
        verifier={"success": False, "reason": "not collected", "checks": []},
        steps=2,
        duration_sec=3.5,
        model_usage=LIVE_SCRIPT.LiveModelUsage(
            model_call_count=2,
            input_tokens=20,
            output_tokens=4,
            total_tokens=24,
        ),
    )
    LIVE_SCRIPT._write_checkpoint_payload(
        path=path,
        stage="executing",
        job_id="job-1",
        task_ids=["harvest_1_dirt", "combat_chicken"],
        task_waves=[["harvest_1_dirt"], ["combat_chicken"]],
        database_url="sqlite+pysqlite:///checkpoint.sqlite3",
        completed_wave_count=1,
        attempt_outcomes=[outcome],
        skill_snapshot_revision="skills-v1",
        learning_snapshot_revision="learning-v1",
    )

    payload = LIVE_SCRIPT._load_checkpoint_payload(path, True)
    state = LIVE_SCRIPT._resume_state_from_checkpoint(
        payload,
        task_ids=["harvest_1_dirt", "combat_chicken"],
        task_waves=[["harvest_1_dirt"], ["combat_chicken"]],
        database_url="sqlite+pysqlite:///checkpoint.sqlite3",
    )

    assert state is not None
    assert state.completed_wave_count == 1
    assert state.attempt_outcomes == (outcome,)
    assert state.skill_snapshot_revision == "skills-v1"


def test_recording_validation_detects_window_crop_changes() -> None:
    """A moved or resized game window invalidates a static ffmpeg crop."""

    original = {"x": 0, "y": 0, "width": 1280, "height": 720}

    assert LIVE_SCRIPT._window_bounds_changed(original, dict(original)) is False
    assert (
        LIVE_SCRIPT._window_bounds_changed(
            original,
            {"x": 10, "y": 0, "width": 1280, "height": 720},
        )
        is True
    )


@pytest.mark.anyio
async def test_explicit_matching_filter_preserves_trusted_window_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit crop must not erase the trusted window identity established in preflight."""

    class FakeRecorder:
        """Recorder double that captures lifecycle calls without invoking ffmpeg."""

        def __init__(self, *, output_path: Path, **_kwargs: Any) -> None:
            self.output_path = output_path
            self.command = ["ffmpeg", str(output_path)]

        def start(self) -> None:
            """Create a non-empty stand-in artifact."""

            self.output_path.write_bytes(b"video")

        def stop(self) -> int:
            """Return a successful ffmpeg-style status."""

            return 0

    monkeypatch.setattr(LIVE_SCRIPT, "AgentScreenRecorder", FakeRecorder)
    monkeypatch.setattr(
        LIVE_SCRIPT,
        "_recording_validation_payload",
        lambda **_kwargs: {
            "valid": True,
            "trusted_minecraft_window": True,
            "reasons": [],
        },
    )
    event_stream = LIVE_SCRIPT.LiveRunEventStream("HarnessTrainer1")
    worker = LIVE_SCRIPT.LiveWorkerSpec(
        worker_id="worker-1",
        worker_url="ws://127.0.0.1:8765",
        username="HarnessTrainer1",
    )
    args = SimpleNamespace(
        record_agent_video=True,
        spectator_player=None,
        recording_window_title="Minecraft",
        recording_window_owner=None,
        recording_window_scale=2.0,
        recording_filter="crop=1280:720:0:0",
        recording_input="Capture screen 0:none",
        recording_fps=30,
        recording_video_size=None,
        recording_no_restore_spectator=False,
        agent_visual_snapshots=False,
    )
    setup = LIVE_SCRIPT.VisualCaptureSetup(
        provider=None,
        preflight={
            "matched": True,
            "trusted_minecraft_window": True,
            "window": {
                "window_id": 7,
                "bounds": {"x": 0, "y": 0, "width": 640, "height": 360},
            },
            "filter": "crop=1280:720:0:0",
        },
        video_filter="crop=1280:720:0:0",
        artifact_dir=str(tmp_path),
    )

    _, session = await LIVE_SCRIPT._run_training_with_optional_recording(
        runner=FakeTrainingRunner(event_stream),
        task_ids=["creative:test"],
        args=args,
        worker_specs=[worker],
        rcon_executor=None,
        recording_output_path=tmp_path / "agent.mp4",
        visual_capture_setup=setup,
        run_event_stream=event_stream,
    )

    assert session["window_capture"]["trusted_minecraft_window"] is True
    assert "skipped" not in session["window_capture"]
    assert session["ffmpeg_started"] is True
