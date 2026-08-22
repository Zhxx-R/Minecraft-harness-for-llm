from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import mc_agent_harness.launcher.service as launcher_service_module
from mc_agent_harness.core.config import PROJECT_ROOT, Settings
from mc_agent_harness.launcher.catalog import ExecutableTask
from mc_agent_harness.launcher.service import (
    LaunchConfiguration,
    LaunchJob,
    QuickStartService,
)


def test_programmatic_command_is_allowlisted_and_uses_full_random_reset(
    tmp_path: Path,
) -> None:
    """Programmatic launches use one worker and reset-time random teleport."""

    service = _service(tmp_path)
    task = ExecutableTask(
        task_id="harvest_oak_log",
        kind="programmatic",
        manifest_path=tmp_path / "programmatic.jsonl",
        manifest={"task_id": "harvest_oak_log", "goal": "Collect oak log"},
    )
    job = _job(tmp_path, task)

    command = service.build_command(job, task, _configuration())

    assert command[1].endswith("run_week10_live_training.py")
    assert command[command.index("--worker-concurrency") + 1] == "1"
    assert "--rcon-random-teleport-on-reset" in command
    assert "--rcon-random-teleport-when-biome-missing" not in command
    assert "--record-agent-video" not in command


def test_creative_command_records_agent_pov_and_runs_offline_scorer(
    tmp_path: Path,
) -> None:
    """Creative launches always use the evidence-producing Week11 wrapper."""

    service = _service(tmp_path)
    task = ExecutableTask(
        task_id="creative:1",
        kind="creative",
        manifest_path=tmp_path / "creative.jsonl",
        manifest={"task_id": "creative:1", "goal": "Build a small shelter"},
    )
    job = _job(tmp_path, task)

    command = service.build_command(job, task, _configuration())

    assert command[1].endswith("run_week11_creative_task.py")
    assert "--spectator-player" in command
    assert "--manage-local-scorer" in command
    assert "--random-teleport" in command
    assert command[command.index("--rcon-host") + 1] == "127.0.0.1"
    assert command[command.index("--output-dir") + 1] == str(job.log_path.parent)


def test_public_config_never_exposes_credentials(tmp_path: Path) -> None:
    """Frontend defaults report secret presence without returning secret values."""

    service = _service(tmp_path)

    payload = service.public_config()

    assert payload["rcon_password_configured"] is True
    assert payload["model_configured"] is True
    assert "rcon_password" not in payload
    assert "qwen_api_key" not in payload


def test_child_environment_propagates_all_spectator_settings(tmp_path: Path) -> None:
    """Quick Start explicitly forwards Settings even when the parent shell did not export them."""

    settings = Settings(
        _env_file=None,
        artifact_root=str(tmp_path / "runs"),
        qwen_api_key="test-key",
        qwen_base_url="https://example.invalid/v1",
        minecraft_rcon_password="test-rcon",
        mc_agent_spectator_player="settings-viewer",
        mc_agent_recording_window_title="Minecraft Test",
        mc_agent_spectator_wait_sec=123.5,
        mc_agent_spectator_chunk_sync_delay_sec=1.25,
        mc_agent_spectator_rebind_interval_sec=4.5,
        mc_agent_spectator_full_sync_interval_sec=18.0,
        mc_agent_spectator_resync_distance_blocks=48.0,
        mc_agent_spectator_resync_cooldown_sec=12.0,
        mc_agent_stop_server_after_run=False,
    )
    service = QuickStartService(settings, project_root=PROJECT_ROOT)
    base_environment = {"KEEP_ME": "present"}

    environment = service._build_child_environment(
        _configuration(),
        base_environment=base_environment,
    )

    assert base_environment == {"KEEP_ME": "present"}
    assert environment["KEEP_ME"] == "present"
    assert environment["MC_AGENT_SPECTATOR_PLAYER"] == "viewer"
    assert environment["MC_AGENT_RECORDING_WINDOW_TITLE"] == "Minecraft Test"
    assert environment["MC_AGENT_SPECTATOR_WAIT_SEC"] == "123.5"
    assert environment["MC_AGENT_SPECTATOR_CHUNK_SYNC_DELAY_SEC"] == "1.25"
    assert environment["MC_AGENT_SPECTATOR_REBIND_INTERVAL_SEC"] == "4.5"
    assert environment["MC_AGENT_SPECTATOR_FULL_SYNC_INTERVAL_SEC"] == "18.0"
    assert environment["MC_AGENT_SPECTATOR_RESYNC_DISTANCE_BLOCKS"] == "48.0"
    assert environment["MC_AGENT_SPECTATOR_RESYNC_COOLDOWN_SEC"] == "12.0"
    assert environment["MC_AGENT_STOP_SERVER_AFTER_RUN"] == "0"


def test_preflight_allows_deferred_local_server_and_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An installed local server and manual client join are pending, not blockers."""

    service = _service(tmp_path)
    task = _task(tmp_path, kind="creative")

    async def unreachable(_host: str, _port: int) -> bool:
        return False

    monkeypatch.setattr(launcher_service_module, "_tcp_reachable", unreachable)
    monkeypatch.setattr(
        service,
        "_managed_server_prerequisites",
        lambda _configuration: (True, "managed server will start"),
    )
    monkeypatch.setattr(service, "_managed_server_pid_alive", lambda: False)

    preflight = asyncio.run(service.preflight(task, _configuration()))
    states = {check["name"]: check["state"] for check in preflight.checks}

    assert preflight.launchable is True
    assert preflight.runtime_ready is False
    assert states["minecraft_server"] == "pending"
    assert states["rcon"] == "pending"
    assert states["minecraft_client"] == "pending"
    assert states["model_provider"] == "ready"
    assert states["view_mode"] == "ready"


def test_preflight_blocks_unreachable_remote_server(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Quick Start never creates a local server for an unreachable remote target."""

    service = _service(tmp_path)
    task = _task(tmp_path, kind="creative")

    async def unreachable(_host: str, _port: int) -> bool:
        return False

    monkeypatch.setattr(launcher_service_module, "_tcp_reachable", unreachable)
    monkeypatch.setattr(
        service,
        "_managed_server_prerequisites",
        lambda _configuration: (False, "loopback only"),
    )
    configuration = replace(
        _configuration(),
        server_host="minecraft.example.invalid",
        rcon_host="minecraft.example.invalid",
    )

    preflight = asyncio.run(service.preflight(task, configuration))

    assert preflight.launchable is False
    assert preflight.runtime_ready is False
    assert (
        next(check for check in preflight.checks if check["name"] == "minecraft_server")["state"]
        == "blocked"
    )


def test_preparation_waits_for_client_before_spawning_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The task process, and therefore recording, starts only after RCON sees the client."""

    service = _service(tmp_path)
    task = _task(tmp_path, kind="creative")
    job = _job(tmp_path, task)
    service._jobs[job.job_id] = job
    order: list[str] = []

    monkeypatch.setattr(
        service,
        "_ensure_server",
        lambda _job, _configuration: order.append("start_server") or True,
    )
    monkeypatch.setattr(
        service,
        "_wait_for_server_endpoints",
        lambda _job, _configuration: order.append("wait_endpoints"),
    )
    monkeypatch.setattr(
        service,
        "_wait_for_client",
        lambda _job, _configuration: order.append("wait_client") or {"viewer"},
    )

    def spawn(_job: LaunchJob, _task: ExecutableTask, _configuration: LaunchConfiguration) -> None:
        order.append("spawn_task")
        _job.status = "running"

    monkeypatch.setattr(service, "_spawn_job", spawn)

    service._prepare_and_spawn_job(job.job_id, task, _configuration())

    assert order == ["start_server", "wait_endpoints", "wait_client", "spawn_task"]
    assert job.server_started_by_job is True
    assert job.status == "running"
    assert "Recording has not started" in job.log_path.read_text(encoding="utf-8")


def test_cancel_waiting_job_stays_active_until_cleanup(tmp_path: Path) -> None:
    """Cancellation does not release the single-server lock before cleanup finishes."""

    service = _service(tmp_path)
    task = _task(tmp_path, kind="creative")
    job = _job(tmp_path, task)
    job.status = "waiting_for_client"
    service._jobs[job.job_id] = job

    cancelled = service.cancel_job(job.job_id)

    assert cancelled.status == "cancelling"
    assert cancelled.cancel_event.is_set()
    assert service.active_job() is cancelled


def _service(tmp_path: Path) -> QuickStartService:
    """Create a launcher with isolated artifacts and explicit provider settings."""

    settings = Settings(
        _env_file=None,
        artifact_root=str(tmp_path / "runs"),
        qwen_api_key="test-key",
        qwen_base_url="https://example.invalid/v1",
        minecraft_rcon_password="test-rcon",
        mc_agent_spectator_player="viewer",
    )
    return QuickStartService(settings, project_root=PROJECT_ROOT)


def _configuration() -> LaunchConfiguration:
    """Return one representative local launch configuration."""

    return LaunchConfiguration(
        view_mode="agent",
        client_player="viewer",
        server_host="127.0.0.1",
        server_port=25565,
        rcon_host="127.0.0.1",
        rcon_port=25575,
        max_steps=30,
        max_runtime_sec=600,
        threat_pause=True,
        random_spawn=True,
        auto_promote=True,
    )


def _task(tmp_path: Path, *, kind: str) -> ExecutableTask:
    """Create one representative task without reading the real catalog."""

    return ExecutableTask(
        task_id="creative:1" if kind == "creative" else "harvest_oak_log",
        kind=kind,
        manifest_path=tmp_path / f"{kind}.jsonl",
        manifest={
            "task_id": "creative:1" if kind == "creative" else "harvest_oak_log",
            "goal": "Build a small shelter" if kind == "creative" else "Collect oak log",
        },
    )


def _job(tmp_path: Path, task: ExecutableTask) -> LaunchJob:
    """Create an unstarted public job record for deterministic command assertions."""

    artifact_dir = tmp_path / "job"
    artifact_dir.mkdir()
    return LaunchJob(
        job_id="launch-test",
        task_id=task.task_id,
        task_kind=task.kind,
        task_goal=str(task.manifest["goal"]),
        view_mode="agent",
        client_player="viewer",
        server_host="127.0.0.1",
        server_port=25565,
        status="starting",
        artifact_dir=str(artifact_dir),
        log_path=artifact_dir / "launcher.log",
    )
