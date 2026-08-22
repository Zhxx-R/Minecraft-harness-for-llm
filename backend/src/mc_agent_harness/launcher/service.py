from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mc_agent_harness.core.config import PROJECT_ROOT, Settings
from mc_agent_harness.launcher.catalog import ExecutableTask
from mc_agent_harness.runtime.server_commands import RconServerCommandExecutor


ViewMode = Literal["agent", "player"]
JobStatus = Literal[
    "starting_server",
    "waiting_for_client",
    "starting",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
ACTIVE_JOB_STATUSES = frozenset(
    {"starting_server", "waiting_for_client", "starting", "running", "cancelling"}
)


@dataclass(frozen=True, slots=True)
class LaunchConfiguration:
    """Validated local launch settings supplied by the dashboard control surface."""

    view_mode: ViewMode
    client_player: str
    server_host: str
    server_port: int
    rcon_host: str
    rcon_port: int
    max_steps: int
    max_runtime_sec: int
    threat_pause: bool = True
    random_spawn: bool = True
    auto_promote: bool = False


@dataclass(frozen=True, slots=True)
class LaunchPreflight:
    """Concrete readiness evidence collected before a local task process starts."""

    launchable: bool
    runtime_ready: bool
    minecraft_reachable: bool
    rcon_configured: bool
    rcon_reachable: bool
    model_configured: bool
    client_online: bool
    online_players: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Convert immutable readiness evidence into an API-safe payload."""

        return {
            **asdict(self),
            "online_players": list(self.online_players),
            "checks": [dict(check) for check in self.checks],
        }


@dataclass(slots=True)
class LaunchJob:
    """One controlled child process and its durable dashboard-visible state."""

    job_id: str
    task_id: str
    task_kind: str
    task_goal: str
    view_mode: ViewMode
    client_player: str
    server_host: str
    server_port: int
    status: JobStatus
    artifact_dir: str
    log_path: Path
    status_detail: str | None = None
    command: list[str] = field(default_factory=list)
    process: subprocess.Popen[bytes] | None = None
    pid: int | None = None
    return_code: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    server_started_by_job: bool = False
    cancel_event: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )

    def to_json(self) -> dict[str, Any]:
        """Return public job state without private paths, credentials, or process objects."""

        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "task_kind": self.task_kind,
            "task_goal": self.task_goal,
            "view_mode": self.view_mode,
            "client_player": self.client_player,
            "server_host": self.server_host,
            "server_port": self.server_port,
            "status": self.status,
            "status_detail": self.status_detail,
            "artifact_dir": self.artifact_dir,
            "pid": self.pid,
            "return_code": self.return_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "server_started_by_job": self.server_started_by_job,
        }


class LaunchConflictError(RuntimeError):
    """Raised when a second quick-start job would contend for the same local server."""


class LaunchPreflightError(RuntimeError):
    """Raised when concrete environment checks reject a requested launch."""

    def __init__(self, preflight: LaunchPreflight) -> None:
        """Retain structured readiness evidence for the API error response."""

        super().__init__("Quick-start preflight failed.")
        self.preflight = preflight


class _LaunchCancelled(RuntimeError):
    """Internal control flow raised when a preparing job is cancelled."""


class QuickStartService:
    """Whitelist-only local launcher for one Minecraft task process at a time."""

    def __init__(
        self,
        settings: Settings,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        """Configure trusted scripts, artifact ownership, and in-memory job tracking."""

        self.settings = settings
        self.project_root = project_root.expanduser().resolve()
        self.python = self.project_root / "backend" / ".venv" / "bin" / "python"
        self.programmatic_script = self.project_root / "scripts" / "run_week10_live_training.py"
        self.creative_script = self.project_root / "scripts" / "run_week11_creative_task.py"
        self.server_start_script = self.project_root / "scripts" / "start_minecraft_server.sh"
        self.server_stop_script = self.project_root / "scripts" / "stop_minecraft_server.sh"
        self.server_dir = self.project_root / "infra" / "minecraft-server"
        self.server_pid_file = self.server_dir / "server.pid"
        self.server_properties = self.server_dir / "server.properties"
        self.artifact_root = Path(settings.artifact_root).expanduser().resolve()
        self.job_root = self.artifact_root / "quick-start"
        self.job_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, LaunchJob] = {}

    def public_config(self) -> dict[str, Any]:
        """Expose safe launch defaults and secret-presence flags to the local dashboard."""

        active = self.active_job()
        return {
            "server_host": self.settings.minecraft_host,
            "server_port": self.settings.minecraft_port,
            "rcon_host": self.settings.minecraft_rcon_host,
            "rcon_port": self.settings.minecraft_rcon_port,
            "default_client_player": self.settings.mc_agent_spectator_player,
            "recording_window_title": self.settings.mc_agent_recording_window_title,
            "rcon_password_configured": bool(self.settings.minecraft_rcon_password),
            "model_configured": bool(self.settings.qwen_api_key and self.settings.qwen_base_url),
            "active_job_id": active.job_id if active is not None else None,
        }

    async def preflight(
        self,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> LaunchPreflight:
        """Classify immediate readiness, deferred local setup, and hard blockers."""

        checks: list[dict[str, Any]] = []
        minecraft_reachable = await _tcp_reachable(
            configuration.server_host,
            configuration.server_port,
        )
        managed_server_available, managed_server_detail = self._managed_server_prerequisites(
            configuration
        )
        managed_server_running = self._managed_server_pid_alive()
        server_state = (
            "ready" if minecraft_reachable else "pending" if managed_server_available else "blocked"
        )
        checks.append(
            _preflight_check(
                "minecraft_server",
                server_state,
                (
                    f"{configuration.server_host}:{configuration.server_port} is ready."
                    if minecraft_reachable
                    else managed_server_detail
                ),
            )
        )

        rcon_configured = bool(self.settings.minecraft_rcon_password)
        online_players: tuple[str, ...] = ()
        rcon_reachable = False
        rcon_detail = "MINECRAFT_RCON_PASSWORD is not configured in the backend."
        if rcon_configured and minecraft_reachable:
            executor = RconServerCommandExecutor(
                host=configuration.rcon_host,
                port=configuration.rcon_port,
                password=str(self.settings.minecraft_rcon_password),
                timeout_sec=3,
            )
            result = (await executor.execute_many(["/list"]))[0]
            rcon_reachable = result.ok
            if result.ok:
                online_players = tuple(sorted(_parse_online_players(result.response)))
            rcon_detail = result.response if result.ok else result.error or result.response
        elif rcon_configured and managed_server_available:
            rcon_detail = "RCON will be checked after the managed Minecraft server starts."
        rcon_can_become_ready = (
            rcon_configured
            and managed_server_available
            and (not minecraft_reachable or managed_server_running)
        )
        rcon_state = (
            "ready" if rcon_reachable else "pending" if rcon_can_become_ready else "blocked"
        )
        checks.append(_preflight_check("rcon", rcon_state, rcon_detail))

        client_online = configuration.client_player in online_players
        client_can_be_waited_for = rcon_reachable or rcon_state == "pending"
        client_state = (
            "ready" if client_online else "pending" if client_can_be_waited_for else "blocked"
        )
        checks.append(
            _preflight_check(
                "minecraft_client",
                client_state,
                (
                    f"{configuration.client_player} is online."
                    if client_online
                    else (
                        f"Start the workflow, then join "
                        f"{configuration.server_host}:{configuration.server_port} as "
                        f"{configuration.client_player}; recording waits for this player."
                        if client_can_be_waited_for
                        else (
                            f"{configuration.client_player} is not online; "
                            f"online players={list(online_players)}"
                        )
                    )
                ),
            )
        )

        model_configured = bool(self.settings.qwen_api_key and self.settings.qwen_base_url)
        checks.append(
            _preflight_check(
                "model_provider",
                "ready" if model_configured else "blocked",
                (
                    "Qwen provider is configured."
                    if model_configured
                    else "QWEN_API_KEY or QWEN_BASE_URL is missing."
                ),
            )
        )
        compatible_view = not (task.kind == "creative" and configuration.view_mode != "agent")
        checks.append(
            _preflight_check(
                "view_mode",
                "ready" if compatible_view else "blocked",
                (
                    "View mode is compatible with this task."
                    if compatible_view
                    else "Creative tasks require Agent POV for trusted video evidence."
                ),
            )
        )

        runtime_ready = all(
            (
                minecraft_reachable,
                rcon_configured,
                rcon_reachable,
                client_online,
                model_configured,
                compatible_view,
            )
        )
        launchable = all(check["state"] != "blocked" for check in checks)
        return LaunchPreflight(
            launchable=launchable,
            runtime_ready=runtime_ready,
            minecraft_reachable=minecraft_reachable,
            rcon_configured=rcon_configured,
            rcon_reachable=rcon_reachable,
            model_configured=model_configured,
            client_online=client_online,
            online_players=online_players,
            checks=tuple(checks),
        )

    async def start_job(
        self,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> tuple[LaunchJob, LaunchPreflight]:
        """Reserve one workflow, prepare its server, then wait for the visible client."""

        preflight = await self.preflight(task, configuration)
        if not preflight.launchable:
            raise LaunchPreflightError(preflight)
        with self._lock:
            active = self.active_job()
            if active is not None:
                raise LaunchConflictError(
                    f"Quick-start job {active.job_id} is already {active.status}."
                )
            job = self._create_job(task, configuration)
            if preflight.minecraft_reachable and preflight.rcon_reachable:
                if preflight.client_online:
                    job.status = "starting"
                    job.status_detail = "Runtime is ready; starting the agent task."
                else:
                    job.status = "waiting_for_client"
                    job.status_detail = (
                        f"Join {configuration.server_host}:{configuration.server_port} as "
                        f"{configuration.client_player}."
                    )
            self._jobs[job.job_id] = job
            self._persist_job(job)
            try:
                threading.Thread(
                    target=self._prepare_and_spawn_job,
                    args=(job.job_id, task, configuration),
                    name=f"quick-start-prepare-{job.job_id}",
                    daemon=True,
                ).start()
            except Exception as exc:
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = datetime.now(tz=UTC).isoformat()
                self._persist_job(job)
                raise
        return job, preflight

    def list_jobs(self) -> list[LaunchJob]:
        """Return recent in-process jobs ordered newest first."""

        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> LaunchJob:
        """Return one known launch job or raise a lookup error."""

        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def active_job(self) -> LaunchJob | None:
        """Return the one running local task, if any."""

        with self._lock:
            return next(
                (job for job in self._jobs.values() if job.status in ACTIVE_JOB_STATUSES),
                None,
            )

    def cancel_job(self, job_id: str) -> LaunchJob:
        """Request cancellation without releasing the server until cleanup completes."""

        job = self.get_job(job_id)
        with self._lock:
            process = job.process
            if job.status not in ACTIVE_JOB_STATUSES:
                return job
            job.cancel_event.set()
            job.status = "cancelling"
            job.status_detail = "Stopping the task and cleaning up its managed server."
            self._persist_job(job)
        self._append_job_log(job, "Cancellation requested.")
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return job

    def read_logs(
        self,
        job_id: str,
        *,
        offset: int = 0,
        max_bytes: int = 65_536,
    ) -> tuple[str, int, bool]:
        """Read a bounded UTF-8 log segment from a caller-controlled byte offset."""

        job = self.get_job(job_id)
        if not job.log_path.is_file():
            return "", offset, job.status not in ACTIVE_JOB_STATUSES
        file_size = job.log_path.stat().st_size
        safe_offset = min(max(offset, 0), file_size)
        with job.log_path.open("rb") as stream:
            stream.seek(safe_offset)
            chunk = stream.read(max_bytes)
            next_offset = stream.tell()
        return (
            chunk.decode("utf-8", errors="replace"),
            next_offset,
            job.status not in ACTIVE_JOB_STATUSES and next_offset >= file_size,
        )

    def _prepare_and_spawn_job(
        self,
        job_id: str,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> None:
        """Prepare the local server and visible client before starting the paid task."""

        job = self.get_job(job_id)
        try:
            server_started = self._ensure_server(job, configuration)
            with self._lock:
                job.server_started_by_job = server_started
                self._persist_job(job)
            self._wait_for_server_endpoints(job, configuration)
            self._raise_if_cancelled(job)
            self._set_job_status(
                job,
                "waiting_for_client",
                (
                    f"Join {configuration.server_host}:{configuration.server_port} as "
                    f"{configuration.client_player}. Recording has not started."
                ),
            )
            online_players = self._wait_for_client(job, configuration)
            self._append_job_log(
                job,
                (
                    f"Client {configuration.client_player} detected; "
                    f"online players={sorted(online_players)}."
                ),
            )
            self._raise_if_cancelled(job)
            self._set_job_status(
                job,
                "starting",
                (
                    "Client detected. Starting task reset and spectator binding; "
                    "recording begins only after the camera is ready."
                ),
            )
            self._raise_if_cancelled(job)
            self._spawn_job(job, task, configuration)
        except _LaunchCancelled:
            self._finish_preparation(job, cancelled=True)
        except Exception as exc:  # noqa: BLE001 - publish all setup failures to the dashboard.
            self._finish_preparation(job, error=exc)

    def _ensure_server(
        self,
        job: LaunchJob,
        configuration: LaunchConfiguration,
    ) -> bool:
        """Start the installed local server when no ready server is already available."""

        game_ready = _tcp_reachable_now(
            configuration.server_host,
            configuration.server_port,
        )
        rcon_ready = _tcp_reachable_now(
            configuration.rcon_host,
            configuration.rcon_port,
        )
        if game_ready and rcon_ready:
            self._append_job_log(
                job,
                (
                    f"Reusing Minecraft server at "
                    f"{configuration.server_host}:{configuration.server_port}."
                ),
            )
            return False
        available, detail = self._managed_server_prerequisites(configuration)
        if not available:
            raise RuntimeError(detail)
        self._set_job_status(
            job,
            "starting_server",
            (
                f"Starting local Minecraft server at "
                f"{configuration.server_host}:{configuration.server_port}."
            ),
        )
        if self._managed_server_pid_alive():
            self._append_job_log(
                job,
                "A managed Minecraft server process is already starting; waiting for it.",
            )
            return False
        self._raise_if_cancelled(job)
        self._append_job_log(job, "Starting the managed Minecraft server.")
        with job.log_path.open("ab", buffering=0) as log_stream:
            subprocess.run(
                [str(self.server_start_script), "--background"],
                cwd=self.project_root,
                env=dict(os.environ),
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=30,
            )
        if not self._managed_server_pid_alive():
            raise RuntimeError(
                "The managed Minecraft start command completed without a live server PID."
            )
        return True

    def _wait_for_server_endpoints(
        self,
        job: LaunchJob,
        configuration: LaunchConfiguration,
        *,
        timeout_sec: float = 120.0,
    ) -> None:
        """Wait for both game and RCON TCP endpoints without starting the agent."""

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            self._raise_if_cancelled(job)
            game_ready = _tcp_reachable_now(
                configuration.server_host,
                configuration.server_port,
            )
            rcon_ready = _tcp_reachable_now(
                configuration.rcon_host,
                configuration.rcon_port,
            )
            if game_ready and rcon_ready:
                self._append_job_log(
                    job,
                    (
                        f"Minecraft game and RCON endpoints are ready at "
                        f"{configuration.server_host}:{configuration.server_port} and "
                        f"{configuration.rcon_host}:{configuration.rcon_port}."
                    ),
                )
                return
            job.cancel_event.wait(1.0)
        raise TimeoutError(
            "Minecraft game and RCON endpoints did not become ready within "
            f"{timeout_sec:.0f} seconds."
        )

    def _wait_for_client(
        self,
        job: LaunchJob,
        configuration: LaunchConfiguration,
    ) -> set[str]:
        """Poll RCON until the configured visible client joins the server."""

        executor = RconServerCommandExecutor(
            host=configuration.rcon_host,
            port=configuration.rcon_port,
            password=str(self.settings.minecraft_rcon_password),
            timeout_sec=5,
        )
        timeout_sec = max(1.0, float(self.settings.mc_agent_spectator_wait_sec))
        deadline = time.monotonic() + timeout_sec
        last_error = "player was not listed"
        while time.monotonic() < deadline:
            self._raise_if_cancelled(job)
            try:
                result = asyncio.run(executor.execute_many(["/list"]))[0]
            except Exception as exc:  # noqa: BLE001 - retry transient RCON startup failures.
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if result.ok:
                    online_players = _parse_online_players(result.response)
                    if configuration.client_player in online_players:
                        return online_players
                    last_error = f"online players={sorted(online_players)}"
                else:
                    last_error = result.error or result.response or "RCON /list failed"
            job.cancel_event.wait(2.0)
        raise TimeoutError(
            f"Minecraft player {configuration.client_player!r} did not join within "
            f"{timeout_sec:.0f} seconds ({last_error})."
        )

    def _managed_server_prerequisites(
        self,
        configuration: LaunchConfiguration,
    ) -> tuple[bool, str]:
        """Validate that the requested endpoint matches the installed local server."""

        if not (
            _is_loopback_host(configuration.server_host)
            and _is_loopback_host(configuration.rcon_host)
        ):
            return (
                False,
                "Automatic Minecraft server startup is available only for loopback hosts.",
            )
        properties = _read_properties(self.server_properties)
        try:
            configured_game_port = int(properties.get("server-port", "25565"))
            configured_rcon_port = int(properties.get("rcon.port", "25575"))
        except ValueError:
            return False, "Managed Minecraft server ports are invalid in server.properties."
        if (
            configuration.server_port != configured_game_port
            or configuration.rcon_port != configured_rcon_port
        ):
            return (
                False,
                (
                    "Automatic startup requires the installed server ports "
                    f"{configured_game_port}/{configured_rcon_port}, not "
                    f"{configuration.server_port}/{configuration.rcon_port}."
                ),
            )
        if properties.get("enable-rcon", "").lower() != "true":
            return False, "The managed Minecraft server does not have RCON enabled."
        if not self.server_start_script.is_file() or not os.access(
            self.server_start_script,
            os.X_OK,
        ):
            return False, "The managed Minecraft start script is missing or not executable."
        server_jar = (
            self.server_dir / "fabric-server-launch.jar"
            if (self.server_dir / "fabric-server-launch.jar").is_file()
            else self.server_dir / "server-1.20.1.jar"
        )
        if not server_jar.is_file():
            return False, "The managed Minecraft server jar is missing."
        eula_path = self.server_dir / "eula.txt"
        if "eula=true" not in (
            eula_path.read_text(encoding="utf-8").lower() if eula_path.is_file() else ""
        ):
            return False, "The managed Minecraft EULA has not been accepted."
        if shutil.which("java") is None:
            return False, "Java is not installed or is not available on PATH."
        return (
            True,
            (
                f"The installed local server will start automatically at "
                f"{configuration.server_host}:{configuration.server_port}."
            ),
        )

    def _managed_server_pid_alive(self) -> bool:
        """Return whether the managed server PID file identifies a live process."""

        try:
            pid = int(self.server_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
            return False
        return True

    def _set_job_status(
        self,
        job: LaunchJob,
        status: JobStatus,
        detail: str,
    ) -> None:
        """Publish one non-terminal lifecycle transition and append it to the log."""

        with self._lock:
            job.status = status
            job.status_detail = detail
            self._persist_job(job)
        self._append_job_log(job, f"{status}: {detail}")

    def _append_job_log(self, job: LaunchJob, message: str) -> None:
        """Append one timestamped orchestration message to the shared launcher log."""

        job.log_path.parent.mkdir(parents=True, exist_ok=True)
        with job.log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{datetime.now(tz=UTC).isoformat()}] {message}\n")

    def _raise_if_cancelled(self, job: LaunchJob) -> None:
        """Interrupt preparation as soon as cancellation is requested."""

        if job.cancel_event.is_set():
            raise _LaunchCancelled("Quick-start job was cancelled.")

    def _finish_preparation(
        self,
        job: LaunchJob,
        *,
        cancelled: bool = False,
        error: Exception | None = None,
    ) -> None:
        """Clean up a workflow that ended before or during task process creation."""

        self._cleanup_managed_server(job)
        with self._lock:
            was_cancelled = cancelled or job.cancel_event.is_set()
            job.status = "cancelled" if was_cancelled else "failed"
            job.status_detail = (
                "Launch cancelled and managed resources were cleaned up."
                if was_cancelled
                else "Launch preparation failed before the agent task could start."
            )
            job.error = (
                None if was_cancelled or error is None else f"{type(error).__name__}: {error}"
            )
            job.finished_at = datetime.now(tz=UTC).isoformat()
            job.process = None
            self._persist_job(job)
        if error is not None and not was_cancelled:
            self._append_job_log(job, f"failed: {type(error).__name__}: {error}")

    def _cleanup_managed_server(self, job: LaunchJob) -> None:
        """Stop only a server started by this job when configured to do so."""

        if not (job.server_started_by_job and self.settings.mc_agent_stop_server_after_run):
            return
        self._append_job_log(job, "Stopping the Minecraft server started by this job.")
        try:
            with job.log_path.open("ab", buffering=0) as log_stream:
                subprocess.run(
                    [str(self.server_stop_script)],
                    cwd=self.project_root,
                    env=dict(os.environ),
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=30,
                )
        except Exception as exc:  # noqa: BLE001 - retain the task result if cleanup fails.
            self._append_job_log(
                job,
                f"Managed Minecraft server cleanup failed: {type(exc).__name__}: {exc}",
            )

    def build_command(
        self,
        job: LaunchJob,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> list[str]:
        """Build a fixed executable plus allowlisted arguments for one task kind."""

        artifact_dir = job.log_path.parent
        if task.kind == "creative":
            command = [
                str(self.python),
                str(self.creative_script),
                "--manifest-path",
                str(task.manifest_path),
                "--task-id",
                task.task_id,
                "--host",
                configuration.server_host,
                "--port",
                str(configuration.server_port),
                "--rcon-reset",
                "--rcon-host",
                configuration.rcon_host,
                "--rcon-port",
                str(configuration.rcon_port),
                "--spectator-player",
                configuration.client_player,
                "--recording-window-title",
                self.settings.mc_agent_recording_window_title,
                "--max-steps",
                str(configuration.max_steps),
                "--max-runtime-sec",
                str(configuration.max_runtime_sec),
                "--manage-local-scorer",
                "--output-dir",
                str(artifact_dir),
            ]
            if configuration.random_spawn:
                command.append("--random-teleport")
            if configuration.threat_pause:
                command.append("--threat-pause")
            return command

        command = [
            str(self.python),
            str(self.programmatic_script),
            "--manifest-dir",
            str(task.manifest_path),
            "--task-id",
            task.task_id,
            "--host",
            configuration.server_host,
            "--port",
            str(configuration.server_port),
            "--worker-concurrency",
            "1",
            "--model-concurrency",
            "1",
            "--username-prefix",
            "HarnessQuickStart",
            "--max-steps-per-task",
            str(configuration.max_steps),
            "--max-runtime-sec-per-task",
            str(configuration.max_runtime_sec),
            "--rcon-reset",
            "--rcon-host",
            configuration.rcon_host,
            "--rcon-port",
            str(configuration.rcon_port),
            "--clear-all-inventory-on-reset",
            "--output",
            str(artifact_dir / "live_training.json"),
        ]
        if configuration.random_spawn:
            command.append("--rcon-random-teleport-on-reset")
        if configuration.threat_pause:
            command.append("--threat-pause")
        if configuration.auto_promote:
            command.append("--auto-promote")
        if configuration.view_mode == "agent":
            command.extend(["--spectator-player", configuration.client_player])
        return command

    def _create_job(
        self,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> LaunchJob:
        """Allocate an artifact directory and initial durable job record."""

        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        job_id = f"launch-{timestamp}-{uuid.uuid4().hex[:8]}"
        artifact_dir = self.job_root / job_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        try:
            public_artifact_dir = str(artifact_dir.relative_to(self.project_root))
        except ValueError:
            public_artifact_dir = str(artifact_dir)
        return LaunchJob(
            job_id=job_id,
            task_id=task.task_id,
            task_kind=task.kind,
            task_goal=str(task.manifest.get("goal") or task.task_id),
            view_mode=configuration.view_mode,
            client_player=configuration.client_player,
            server_host=configuration.server_host,
            server_port=configuration.server_port,
            status="starting_server",
            artifact_dir=public_artifact_dir,
            log_path=artifact_dir / "launcher.log",
            status_detail=(
                f"Preparing Minecraft server at "
                f"{configuration.server_host}:{configuration.server_port}."
            ),
        )

    def _spawn_job(
        self,
        job: LaunchJob,
        task: ExecutableTask,
        configuration: LaunchConfiguration,
    ) -> None:
        """Start the child without a shell and attach a daemon completion watcher."""

        self._raise_if_cancelled(job)
        command = self.build_command(job, task, configuration)
        for required_path in (self.python, Path(command[1]), task.manifest_path):
            if not required_path.is_file():
                raise FileNotFoundError(f"Required launch file is missing: {required_path}")
        environment = self._build_child_environment(configuration)
        log_stream = job.log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=environment,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_stream.close()
        with self._lock:
            cancelled_before_publish = job.cancel_event.is_set()
            if not cancelled_before_publish:
                job.command = command
                job.process = process
                job.pid = process.pid
                job.status = "running"
                job.status_detail = (
                    "Agent task is running. Creative recording starts after spectator camera readiness."
                    if task.kind == "creative"
                    else "Agent task is running."
                )
                job.started_at = datetime.now(tz=UTC).isoformat()
                self._persist_job(job)
        if cancelled_before_publish:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            raise _LaunchCancelled("Quick-start job was cancelled before task publication.")
        threading.Thread(
            target=self._watch_job,
            args=(job.job_id,),
            name=f"quick-start-{job.job_id}",
            daemon=True,
        ).start()

    def _build_child_environment(
        self,
        configuration: LaunchConfiguration,
        *,
        base_environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build an explicit child environment from Settings and launch configuration."""

        environment = dict(os.environ if base_environment is None else base_environment)
        environment["PYTHONPATH"] = str(self.project_root / "backend" / "src")
        environment["DATABASE_URL"] = self.settings.database_url
        environment["MINECRAFT_RCON_PASSWORD"] = str(self.settings.minecraft_rcon_password or "")
        environment["MINECRAFT_RCON_HOST"] = configuration.rcon_host
        environment["MINECRAFT_RCON_PORT"] = str(configuration.rcon_port)
        environment["MINECRAFT_HOST"] = configuration.server_host
        environment["MINECRAFT_PORT"] = str(configuration.server_port)
        environment["MC_AGENT_SPECTATOR_PLAYER"] = configuration.client_player
        environment["MC_AGENT_RECORDING_WINDOW_TITLE"] = (
            self.settings.mc_agent_recording_window_title
        )
        environment["MC_AGENT_SPECTATOR_WAIT_SEC"] = str(self.settings.mc_agent_spectator_wait_sec)
        environment["MC_AGENT_SPECTATOR_CHUNK_SYNC_DELAY_SEC"] = str(
            self.settings.mc_agent_spectator_chunk_sync_delay_sec
        )
        environment["MC_AGENT_SPECTATOR_REBIND_INTERVAL_SEC"] = str(
            self.settings.mc_agent_spectator_rebind_interval_sec
        )
        environment["MC_AGENT_SPECTATOR_FULL_SYNC_INTERVAL_SEC"] = str(
            self.settings.mc_agent_spectator_full_sync_interval_sec
        )
        environment["MC_AGENT_SPECTATOR_RESYNC_DISTANCE_BLOCKS"] = str(
            self.settings.mc_agent_spectator_resync_distance_blocks
        )
        environment["MC_AGENT_SPECTATOR_RESYNC_COOLDOWN_SEC"] = str(
            self.settings.mc_agent_spectator_resync_cooldown_sec
        )
        environment["MC_AGENT_STOP_SERVER_AFTER_RUN"] = (
            "1" if self.settings.mc_agent_stop_server_after_run else "0"
        )
        if self.settings.qwen_api_key:
            environment["QWEN_API_KEY"] = self.settings.qwen_api_key
        if self.settings.qwen_base_url:
            environment["QWEN_BASE_URL"] = self.settings.qwen_base_url
        return environment

    def _watch_job(self, job_id: str) -> None:
        """Wait for process completion and atomically publish the terminal status."""

        job = self.get_job(job_id)
        process = job.process
        if process is None:
            return
        return_code = process.wait()
        self._cleanup_managed_server(job)
        with self._lock:
            job.return_code = return_code
            if job.cancel_event.is_set() or job.status == "cancelling":
                job.status = "cancelled"
                job.status_detail = "Task cancelled and managed resources were cleaned up."
            else:
                job.status = "succeeded" if return_code == 0 else "failed"
                job.status_detail = (
                    "Task completed successfully."
                    if return_code == 0
                    else f"Task process exited with code {return_code}."
                )
            job.finished_at = datetime.now(tz=UTC).isoformat()
            job.process = None
            self._persist_job(job)

    def _persist_job(self, job: LaunchJob) -> None:
        """Write an atomic redacted status document beside the task artifacts."""

        payload = {
            "schema_version": "mc-agent-harness.quick-start-job.v2",
            **job.to_json(),
        }
        status_path = job.log_path.parent / "launcher_job.json"
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(status_path)


async def _tcp_reachable(host: str, port: int) -> bool:
    """Return whether a TCP endpoint accepts a short local readiness connection."""

    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=2,
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


def _tcp_reachable_now(host: str, port: int) -> bool:
    """Return whether a TCP endpoint is accepting a short synchronous connection."""

    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _is_loopback_host(host: str) -> bool:
    """Accept explicit localhost names and numeric loopback addresses only."""

    normalized = host.strip().lower().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _read_properties(path: Path) -> dict[str, str]:
    """Parse the simple key/value subset used by Minecraft server.properties."""

    if not path.is_file():
        return {}
    properties: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        properties[key.strip()] = value.strip()
    return properties


def _preflight_check(
    name: str,
    state: Literal["ready", "pending", "blocked"],
    detail: str,
) -> dict[str, Any]:
    """Build one backward-compatible readiness check with explicit tri-state semantics."""

    return {
        "name": name,
        "ok": state == "ready",
        "state": state,
        "blocking": state == "blocked",
        "detail": detail,
    }


def _parse_online_players(response: str) -> set[str]:
    """Extract exact Minecraft player names from the localized RCON list suffix."""

    _prefix, separator, players = response.replace("：", ":").rpartition(":")
    if not separator:
        return set()
    return {value.strip() for value in players.split(",") if value.strip()}
