from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.core.config import settings  # noqa: E402
from mc_agent_harness.db.models import Base, RunRecord  # noqa: E402
from mc_agent_harness.evaluation.benchmark import ScriptedActionProvider  # noqa: E402
from mc_agent_harness.evaluation.mineclip import MineClipScorer  # noqa: E402
from mc_agent_harness.evaluation.progress import (  # noqa: E402
    CreativeProgressFeedbackRuntime,
    CreativeProgressMonitor,
    CreativeProgressPolicy,
)
from mc_agent_harness.evaluation.video import validate_video_artifact  # noqa: E402
from mc_agent_harness.harness.persistent_recorder import (  # noqa: E402
    PersistentEvaluationRecorder,
)
from mc_agent_harness.models.router import (  # noqa: E402
    ModelProfile,
    ModelRouter,
    OpenAICompatibleProvider,
    ResilientModelProvider,
)
from mc_agent_harness.runtime.macos_window_capture import (  # noqa: E402
    MacOSWindowCaptureError,
    MacOSWindowCaptureProvider,
    crop_filter_for_window,
    select_macos_window,
    visible_macos_windows,
)
from mc_agent_harness.runtime.game_runtime import GameRuntime  # noqa: E402
from mc_agent_harness.runtime.mineflayer_client import MineflayerClient  # noqa: E402
from mc_agent_harness.runtime.server_commands import (  # noqa: E402
    RconServerCommandExecutor,
    ServerCommandResetConfig,
)
from mc_agent_harness.runtime.threat_pause import ThreatPauseConfig  # noqa: E402
from mc_agent_harness.runtime.visual_snapshot import VisualSnapshotRuntime  # noqa: E402
from mc_agent_harness.runtime.server_pool import (  # noqa: E402
    MinecraftServerInstanceSpec,
    estimate_server_pool_resources,
    load_server_pool_state,
)
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider  # noqa: E402
from mc_agent_harness.tasks.similarity import (  # noqa: E402
    DiverseBatchPlanner,
    DiverseWavePlan,
    DiverseWavePlanner,
)
from mc_agent_harness.training import (  # noqa: E402
    LiveLearningUpdate,
    LiveMinecraftConfig,
    LiveModelUsage,
    LiveSkillUpdate,
    LiveTrainingConfig,
    LiveTrainingOutcome,
    LiveTrainingRunner,
    LiveTrainingResumeState,
    LiveWorkerSpec,
    RandomTeleportResetConfig,
    TrainingBudget,
)


def parse_args() -> argparse.Namespace:
    """Parse CLI options for live parallel programmatic skill training."""

    parser = argparse.ArgumentParser(
        description="Run live parallel Mineflayer programmatic training and skill updates."
    )
    parser.add_argument("--host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MINECRAFT_PORT", "25565")),
        help="Minecraft port for single-server mode; ignored by server-pool workers.",
    )
    parser.add_argument(
        "--server-pool-state",
        type=Path,
        default=None,
        help="State JSON produced by start_minecraft_server_pool.py for isolated workers.",
    )
    parser.add_argument(
        "--allow-shared-server-workers",
        action="store_true",
        help="Development-only override allowing multiple workers in one Minecraft world.",
    )
    parser.add_argument("--username-prefix", default="HarnessTrainer")
    parser.add_argument("--spawn-timeout-ms", type=int, default=20000)
    parser.add_argument("--worker-concurrency", type=int, default=2)
    parser.add_argument("--task-id", action="append", default=None)
    parser.add_argument("--diverse-batch-size", type=int, default=None)
    parser.add_argument(
        "--stratified-batch",
        action="store_true",
        help="Allocate automatic batch slots proportionally across selected categories.",
    )
    parser.add_argument("--max-task-similarity", type=float, default=0.45)
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Include only these task categories during automatic batch selection.",
    )
    parser.add_argument(
        "--exclude-category",
        action="append",
        default=None,
        help="Exclude these task categories during automatic batch selection.",
    )
    parser.add_argument(
        "--no-diverse-waves",
        action="store_true",
        help="Disable the low-similarity barrier between concurrent task waves.",
    )
    parser.add_argument(
        "--manifest-dir", type=Path, default=ROOT / "tasks" / "manifests"
    )
    parser.add_argument("--max-steps-per-task", type=int, default=8)
    parser.add_argument("--max-runtime-sec-per-task", type=float, default=None)
    parser.add_argument(
        "--model-timeout-retries",
        type=int,
        default=2,
        help="Retry count for LLM provider read timeouts before marking a run inconclusive.",
    )
    parser.add_argument(
        "--model-timeout-backoff-sec",
        action="append",
        type=float,
        default=None,
        help="Backoff seconds before each LLM timeout retry; repeat to provide a sequence.",
    )
    parser.add_argument(
        "--model-timeout-requeues",
        type=int,
        default=1,
        help="Requeue a task this many times after model timeout retries are exhausted.",
    )
    parser.add_argument(
        "--model-timeout-requeue-delay-sec",
        type=float,
        default=10.0,
        help="Delay before requeueing a task after exhausted model timeout retries.",
    )
    parser.add_argument(
        "--worker-failure-requeues",
        type=int,
        default=1,
        help="Restart and requeue a task this many times after an unknown worker action state.",
    )
    parser.add_argument(
        "--max-task-retries",
        type=int,
        default=0,
        help="Retry each failed task this many times after its initial attempt.",
    )
    parser.add_argument(
        "--task-retry-delay-sec",
        type=float,
        default=2.0,
        help="Delay before requeueing ordinary failed or timed-out task attempts.",
    )
    parser.add_argument(
        "--model-concurrency",
        type=int,
        default=2,
        help="Maximum concurrent remote model requests shared by all workers.",
    )
    parser.add_argument(
        "--provider-transient-retries",
        type=int,
        default=2,
        help="Retry count for model-provider HTTP 429 and 5xx responses.",
    )
    parser.add_argument(
        "--provider-retry-backoff-sec",
        action="append",
        type=float,
        default=None,
        help="Backoff seconds for provider 429/5xx retries; repeat for a sequence.",
    )
    parser.add_argument(
        "--start-delay-sec",
        type=float,
        default=0.0,
        help="Wait after each bot spawns and before the first observation/action.",
    )
    parser.add_argument("--duplicate-threshold", type=float, default=0.82)
    parser.add_argument("--auto-promote", action="store_true")
    parser.add_argument(
        "--max-retrieved-skills",
        type=int,
        default=3,
        help=(
            "Maximum promoted skills injected into each model turn. Use 0 for a controlled "
            "no-skill condition."
        ),
    )
    parser.add_argument(
        "--min-skill-relevance",
        type=float,
        default=float(os.getenv("MC_AGENT_MIN_SKILL_RELEVANCE", "0.5")),
        help=(
            "Minimum normalized task relevance required before a promoted Skill may be "
            "injected. Contextual blocker matches such as no_path receive a recovery boost."
        ),
    )
    parser.add_argument(
        "--clear-inventory-on-reset",
        action="store_true",
        help="Clear verifier target items during worker reset before the first observation.",
    )
    parser.add_argument(
        "--clear-item",
        action="append",
        default=None,
        help="Specific item id to clear on reset; repeat for multiple items. Defaults to verifier target items.",
    )
    parser.add_argument(
        "--clear-all-inventory-on-reset",
        action="store_true",
        help="Clear the worker bot's full inventory during reset.",
    )
    parser.add_argument("--clear-inventory-wait-ms", type=int, default=750)
    parser.add_argument(
        "--no-reset-drop-fallback",
        action="store_true",
        help="Disable the non-OP fallback that tosses inventory items when /clear is rejected.",
    )
    parser.add_argument(
        "--rcon-reset",
        action="store_true",
        help="Use server-authorized Minecraft RCON commands for reset cleanup.",
    )
    parser.add_argument("--rcon-host", default=os.getenv("MINECRAFT_RCON_HOST"))
    parser.add_argument(
        "--rcon-port",
        type=int,
        default=int(os.getenv("MINECRAFT_RCON_PORT", "25575")),
    )
    parser.add_argument("--rcon-password", default=os.getenv("MINECRAFT_RCON_PASSWORD"))
    parser.add_argument("--rcon-timeout-sec", type=float, default=3.0)
    parser.add_argument(
        "--rcon-no-clear-drops",
        action="store_true",
        help="Do not run /kill @e[type=item] during RCON reset.",
    )
    parser.add_argument(
        "--rcon-no-restore-player-state",
        action="store_true",
        help="Do not restore player health and hunger during RCON reset.",
    )
    parser.add_argument(
        "--rcon-align-biome",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use /locate biome and /spreadplayers for manifest biome_hint values.",
    )
    parser.add_argument(
        "--rcon-set-time",
        default=None,
        help="Optional RCON reset time value, for example day or 0.",
    )
    parser.add_argument(
        "--rcon-set-weather",
        default=None,
        help="Optional RCON reset weather value, for example clear.",
    )
    parser.add_argument(
        "--rcon-random-teleport-on-reset",
        action="store_true",
        help="Use RCON /spreadplayers to randomly teleport each bot during reset.",
    )
    parser.add_argument(
        "--rcon-random-teleport-when-biome-missing",
        action="store_true",
        help="Randomly teleport only tasks whose MineDojo reset spec has no biome_hint.",
    )
    parser.add_argument(
        "--rcon-random-teleport-center-x",
        type=int,
        default=int(os.getenv("MINECRAFT_RANDOM_TELEPORT_CENTER_X", "0")),
        help="X center used by /spreadplayers when random reset teleport is enabled.",
    )
    parser.add_argument(
        "--rcon-random-teleport-center-z",
        type=int,
        default=int(os.getenv("MINECRAFT_RANDOM_TELEPORT_CENTER_Z", "0")),
        help="Z center used by /spreadplayers when random reset teleport is enabled.",
    )
    parser.add_argument(
        "--rcon-random-teleport-spread-distance",
        type=int,
        default=int(os.getenv("MINECRAFT_RANDOM_TELEPORT_SPREAD_DISTANCE", "0")),
        help="Minimum distance between spread players; usually 0 for one bot.",
    )
    parser.add_argument(
        "--rcon-random-teleport-max-range",
        type=int,
        default=int(os.getenv("MINECRAFT_RANDOM_TELEPORT_MAX_RANGE", "200")),
        help="Maximum random teleport range around the configured center.",
    )
    parser.add_argument(
        "--rcon-random-teleport-keep-start-position",
        action="store_true",
        help="Keep manifest start_position after random teleport; default removes it so /tp does not override /spreadplayers.",
    )
    parser.add_argument(
        "--threat-pause",
        action="store_true",
        help="Freeze server ticks while the model deliberates near hostile entities.",
    )
    parser.add_argument(
        "--threat-pause-distance",
        type=float,
        default=16.0,
        help="Maximum hostile entity distance that triggers threat-aware tick freeze.",
    )
    parser.add_argument(
        "--scripted",
        action="store_true",
        help="Use manifest scripted actions instead of LLM calls for smoke testing.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Select tasks and build diverse waves without starting servers, workers, or models.",
    )
    parser.add_argument("--keep-workers", action="store_true")
    parser.add_argument(
        "--record-agent-video",
        action="store_true",
        help="Record the Minecraft client screen with ffmpeg during the live training run.",
    )
    parser.add_argument(
        "--recording-output",
        type=Path,
        default=None,
        help="MP4 output path for --record-agent-video. Defaults next to --output.",
    )
    parser.add_argument(
        "--recording-input",
        default=os.getenv("MC_AGENT_RECORDING_INPUT", "Capture screen 0:none"),
        help='ffmpeg avfoundation input device, for example "Capture screen 0:none" or "4:none" on macOS.',
    )
    parser.add_argument(
        "--recording-fps",
        type=int,
        default=int(os.getenv("MC_AGENT_RECORDING_FPS", "30")),
    )
    parser.add_argument(
        "--recording-video-size",
        default=os.getenv("MC_AGENT_RECORDING_VIDEO_SIZE"),
        help='Optional ffmpeg capture size, for example "1920x1080".',
    )
    parser.add_argument(
        "--recording-filter",
        default=os.getenv("MC_AGENT_RECORDING_FILTER"),
        help='Optional ffmpeg -vf filter, for example "crop=1280:720:0:0".',
    )
    parser.add_argument(
        "--recording-window-title",
        default=os.getenv("MC_AGENT_RECORDING_WINDOW_TITLE"),
        help="Optional macOS Minecraft window-title substring used to generate a trusted crop.",
    )
    parser.add_argument(
        "--recording-window-owner",
        default=os.getenv("MC_AGENT_RECORDING_WINDOW_OWNER"),
        help="Optional macOS process owner constraint, for example java.",
    )
    parser.add_argument(
        "--recording-window-scale",
        type=float,
        default=float(os.getenv("MC_AGENT_RECORDING_WINDOW_SCALE", "2.0")),
        help="Scale AppleScript window points to captured pixels; Retina Mac displays are usually 2.0.",
    )
    parser.add_argument(
        "--spectator-player",
        default=os.getenv("MC_AGENT_SPECTATOR_PLAYER"),
        help=(
            "Optional Minecraft client player to switch into spectator mode and follow the first "
            "bot, independently of video recording."
        ),
    )
    parser.add_argument("--recording-spectate-retries", type=int, default=20)
    parser.add_argument("--recording-spectate-interval-sec", type=float, default=1.5)
    parser.add_argument(
        "--spectator-chunk-sync-delay-sec",
        type=float,
        default=float(os.getenv("MC_AGENT_SPECTATOR_CHUNK_SYNC_DELAY_SEC", "0.75")),
        help=(
            "Delay after teleporting the spectator beside the bot so the client can track the "
            "target entity before /spectate is sent."
        ),
    )
    parser.add_argument(
        "--spectator-rebind-interval-sec",
        type=float,
        default=float(os.getenv("MC_AGENT_SPECTATOR_REBIND_INTERVAL_SEC", "10.0")),
        help=(
            "Check the latest committed agent camera state at this interval. The harness does "
            "not re-issue /spectate unless a resynchronization condition is detected."
        ),
    )
    parser.add_argument(
        "--spectator-full-sync-interval-sec",
        type=float,
        default=float(os.getenv("MC_AGENT_SPECTATOR_FULL_SYNC_INTERVAL_SEC", "0.0")),
        help=(
            "Periodically teleport the spectator beside the bot, wait for chunks, and reattach "
            "the camera. Zero disables periodic full synchronization."
        ),
    )
    parser.add_argument(
        "--spectator-resync-distance-blocks",
        type=float,
        default=float(os.getenv("MC_AGENT_SPECTATOR_RESYNC_DISTANCE_BLOCKS", "96.0")),
        help=(
            "Soft-resynchronize when the server reports that the spectator is this far from "
            "the agent. Zero disables distance-based synchronization."
        ),
    )
    parser.add_argument(
        "--spectator-resync-cooldown-sec",
        type=float,
        default=float(os.getenv("MC_AGENT_SPECTATOR_RESYNC_COOLDOWN_SEC", "30.0")),
        help=(
            "Minimum delay between distance- or timer-triggered spectator synchronizations. "
            "Agent spawn, entity, or dimension changes bypass this cooldown."
        ),
    )
    parser.add_argument(
        "--recording-no-restore-spectator",
        action="store_true",
        help="Leave --spectator-player in spectator mode when training stops.",
    )
    parser.add_argument(
        "--agent-visual-snapshots",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Implement request_visual_snapshot with direct Minecraft-window captures and inject "
            "the selected frame into the next multimodal model turn."
        ),
    )
    parser.add_argument(
        "--initial-visual-snapshot",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Capture one trusted frame for the first model turn of a visual task.",
    )
    parser.add_argument(
        "--visual-snapshot-dir",
        type=Path,
        default=None,
        help="Optional artifact directory for preflight and model-requested visual frames.",
    )
    parser.add_argument(
        "--mineclip-progress-feedback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Continuously buffer trusted Minecraft frames and asynchronously return advisory "
            "MineCLIP trends after important creative-task actions."
        ),
    )
    parser.add_argument(
        "--mineclip-progress-action",
        action="append",
        default=None,
        help=(
            "World-changing action that queues online MineCLIP feedback; repeat to override "
            "the default place_block/dig_block_at/use_item set."
        ),
    )
    parser.add_argument(
        "--mineclip-progress-sample-fps",
        type=float,
        default=2.0,
        help="Low-rate trusted-window capture frequency for the online ring buffer.",
    )
    parser.add_argument(
        "--mineclip-progress-min-interval-sec",
        type=float,
        default=3.0,
        help="Minimum interval between queued important-action checkpoints.",
    )
    parser.add_argument(
        "--mineclip-progress-post-action-frames",
        type=int,
        default=2,
        help="Frames captured after an action before its 16-frame MineCLIP window is scored.",
    )
    parser.add_argument(
        "--mineclip-progress-scorer-url",
        default=os.getenv("MINECLIP_SCORER_URL", settings.mineclip_scorer_url),
        help="Isolated MineCLIP scorer used by asynchronous process feedback.",
    )
    parser.add_argument(
        "--mineclip-progress-scorer-timeout-sec",
        type=float,
        default=30.0,
        help="HTTP timeout for one asynchronous MineCLIP checkpoint score.",
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--database-path", type=Path, default=None)
    parser.add_argument(
        "--allow-sqlite-parallel",
        action="store_true",
        help="Development-only override allowing a multi-worker run to use SQLite.",
    )
    parser.add_argument("--job-id", default=None)
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=None,
        help="Write an atomic checkpoint after each completed task wave.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume completed waves from --checkpoint-path after validating the plan.",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


@dataclass(slots=True)
class RecordingSession:
    """Audit metadata for one optional agent POV recording session."""

    enabled: bool
    output_path: str | None = None
    ffmpeg_command: list[str] = field(default_factory=list)
    ffmpeg_started: bool = False
    ffmpeg_returncode: int | None = None
    error: str | None = None
    spectator_player: str | None = None
    spectated_username: str | None = None
    spectate_attempts: list[dict[str, Any]] = field(default_factory=list)
    restore_attempts: list[dict[str, Any]] = field(default_factory=list)
    requested_window_title: str | None = None
    window_capture: dict[str, Any] | None = None
    effective_recording_filter: str | None = None
    validation: dict[str, Any] | None = None
    agent_visual_snapshots_enabled: bool = False
    visual_snapshot_dir: str | None = None
    camera_ready_before_recording: bool | None = None
    recording_started_at: str | None = None
    note: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert recording metadata into the live run report payload."""

        return {
            "enabled": self.enabled,
            "output_path": self.output_path,
            "ffmpeg_command": self.ffmpeg_command,
            "ffmpeg_started": self.ffmpeg_started,
            "ffmpeg_returncode": self.ffmpeg_returncode,
            "error": self.error,
            "spectator_player": self.spectator_player,
            "spectated_username": self.spectated_username,
            "spectate_attempts": self.spectate_attempts,
            "restore_attempts": self.restore_attempts,
            "requested_window_title": self.requested_window_title,
            "window_capture": self.window_capture,
            "effective_recording_filter": self.effective_recording_filter,
            "validation": self.validation,
            "agent_visual_snapshots_enabled": self.agent_visual_snapshots_enabled,
            "visual_snapshot_dir": self.visual_snapshot_dir,
            "camera_ready_before_recording": self.camera_ready_before_recording,
            "recording_started_at": self.recording_started_at,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class VisualCaptureSetup:
    """Prepared trusted window target shared by recording and on-demand model vision."""

    provider: MacOSWindowCaptureProvider | None
    preflight: dict[str, Any] | None
    video_filter: str | None
    artifact_dir: str | None


@dataclass(slots=True)
class WorkerProcessSupervisor:
    """Own one local worker process, its durable output log, and bounded restarts."""

    worker_id: str
    port: int
    log_path: Path
    process: subprocess.Popen[str] | None = None
    restart_count: int = 0
    _log_handle: TextIO | None = None

    def start(self) -> None:
        """Start the worker on its stable RPC port with stdout redirected to a file."""

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._log_handle is None or self._log_handle.closed:
            self._log_handle = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._log_handle.write(
            f"\n[{datetime.now(tz=UTC).isoformat()}] starting {self.worker_id} "
            f"on port {self.port}\n"
        )
        self.process = _start_worker(self.port, stdout=self._log_handle)

    def restart(self) -> dict[str, Any]:
        """Terminate the current process group and bring the same RPC endpoint back."""

        previous_pid = self.process.pid if self.process is not None else None
        if self.process is not None:
            _terminate_process(self.process)
        self.restart_count += 1
        self.start()
        _wait_for_tcp("127.0.0.1", self.port, timeout_sec=20)
        return {
            "success": True,
            "worker_id": self.worker_id,
            "previous_pid": previous_pid,
            "pid": self.process.pid if self.process is not None else None,
            "restart_count": self.restart_count,
            "log_path": str(self.log_path),
        }

    def stop(self) -> None:
        """Stop the worker process group and close its output artifact."""

        if self.process is not None:
            _terminate_process(self.process)
            self.process = None
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def to_json(self) -> dict[str, Any]:
        """Return process identity and log metadata for the training report."""

        return {
            "worker_id": self.worker_id,
            "port": self.port,
            "pid": self.process.pid if self.process is not None else None,
            "restart_count": self.restart_count,
            "log_path": str(self.log_path),
        }


@dataclass(frozen=True, slots=True)
class LiveRunStartedEvent:
    """Persisted post-reset run signal used to synchronize the spectator camera."""

    run_id: str
    task_id: str | None
    agent_id: str
    worker_id: str | None


_ENTITY_POSITION_PATTERN = re.compile(
    r"\[\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*[dDfF]?\s*,\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*[dDfF]?\s*,\s*"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)\s*[dDfF]?\s*"
    r"\]"
)


def _camera_position(value: Any) -> tuple[float, float, float] | None:
    """Normalize one observation position into an immutable camera coordinate."""

    if not isinstance(value, dict):
        return None
    try:
        return (float(value["x"]), float(value["y"]), float(value["z"]))
    except (KeyError, TypeError, ValueError):
        return None


def _parse_entity_position_response(
    response: str | None,
) -> tuple[float, float, float] | None:
    """Parse the numeric NBT vector returned by `/data get entity <player> Pos`."""

    if not response:
        return None
    match = _ENTITY_POSITION_PATTERN.search(response)
    if match is None:
        return None
    try:
        x, y, z = (float(value) for value in match.groups())
    except ValueError:
        return None
    return x, y, z


@dataclass(frozen=True, slots=True)
class LiveAgentCameraState:
    """Latest committed agent state used to resynchronize the spectator only when needed."""

    run_id: str
    entity_id: int | None
    spawn_sequence: int | None
    position: tuple[float, float, float] | None
    dimension: str | None

    def to_json(self) -> dict[str, Any]:
        """Return a compact audit payload without exposing the full observation."""

        return {
            "run_id": self.run_id,
            "entity_id": self.entity_id,
            "spawn_sequence": self.spawn_sequence,
            "position": (
                {
                    "x": self.position[0],
                    "y": self.position[1],
                    "z": self.position[2],
                }
                if self.position is not None
                else None
            ),
            "dimension": self.dimension,
        }


class LiveRunEventStream:
    """Queue run starts and retain the latest committed camera state for one visible worker."""

    def __init__(self, target_agent_id: str) -> None:
        """Create an event stream that ignores runs belonging to other workers."""

        self.target_agent_id = target_agent_id
        self._queue: asyncio.Queue[LiveRunStartedEvent] = asyncio.Queue()
        self._camera_states: dict[str, LiveAgentCameraState] = {}

    async def publish(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Publish run starts and camera state only after the database transaction commits."""

        if payload.get("agent_id") != self.target_agent_id:
            return
        if event_type == "run_started":
            await self._queue.put(
                LiveRunStartedEvent(
                    run_id=run_id,
                    task_id=str(payload["task_id"])
                    if payload.get("task_id") is not None
                    else None,
                    agent_id=self.target_agent_id,
                    worker_id=str(payload["worker_id"])
                    if payload.get("worker_id") is not None
                    else None,
                )
            )
            return
        if event_type != "observation":
            return
        observation = payload.get("observation")
        if not isinstance(observation, dict):
            return
        world = observation.get("world")
        raw_entity_id = observation.get("entity_id")
        raw_spawn_sequence = observation.get("spawn_sequence")
        self._camera_states[run_id] = LiveAgentCameraState(
            run_id=run_id,
            entity_id=raw_entity_id if isinstance(raw_entity_id, int) else None,
            spawn_sequence=(
                raw_spawn_sequence if isinstance(raw_spawn_sequence, int) else None
            ),
            position=_camera_position(observation.get("position")),
            dimension=(
                str(world["dimension"])
                if isinstance(world, dict) and world.get("dimension") is not None
                else None
            ),
        )

    async def next(self) -> LiveRunStartedEvent:
        """Wait for the next post-reset run handled by the followed worker."""

        return await self._queue.get()

    def latest_camera_state(self, run_id: str) -> LiveAgentCameraState | None:
        """Return the latest committed state for the active run, if one is available."""

        return self._camera_states.get(run_id)


SpectatorAuditCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class AgentScreenRecorder:
    """Small ffmpeg process wrapper used to capture the visible Minecraft client."""

    def __init__(
        self,
        *,
        output_path: Path,
        input_spec: str,
        fps: int,
        video_size: str | None,
        video_filter: str | None,
    ) -> None:
        self.output_path = output_path
        self.input_spec = input_spec
        self.fps = fps
        self.video_size = video_size
        self.video_filter = video_filter
        self.process: subprocess.Popen[str] | None = None
        self.command: list[str] = []

    def start(self) -> None:
        """Start ffmpeg screen capture and fail fast when the recorder cannot start."""

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RuntimeError(
                "ffmpeg was not found on PATH; install ffmpeg before recording."
            )
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            ffmpeg,
            "-y",
            "-loglevel",
            "warning",
            "-f",
            "avfoundation",
            "-framerate",
            str(max(1, self.fps)),
        ]
        if self.video_size:
            command.extend(["-video_size", self.video_size])
        command.extend(["-i", self.input_spec])
        if self.video_filter:
            command.extend(["-vf", self.video_filter])
        command.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(self.output_path),
            ]
        )
        self.command = command
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.5)
        if self.process.poll() is not None:
            output, _error = self.process.communicate(timeout=2)
            raise RuntimeError(
                output.strip() or "ffmpeg exited before recording started."
            )

    def stop(self) -> int | None:
        """Ask ffmpeg to finish the file cleanly, then terminate if it does not exit."""

        process = self.process
        if process is None:
            return None
        if process.poll() is not None:
            return process.returncode
        if process.stdin is not None:
            try:
                process.stdin.write("q\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return process.returncode


def main() -> None:
    """Start worker processes, run live training, and print a compact JSON summary."""

    args = parse_args()
    _validate_cli_limits(args)
    if args.mineclip_progress_feedback:
        _validate_mineclip_progress_scorer(args)
    if not args.scripted and not args.plan_only:
        _validate_model_environment()

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        args.output or ROOT / "runs" / f"week10_live_training_{timestamp}.json"
    )
    recording_output_path = _recording_output_path(args, output_path)
    provider = MineDojoTaskProvider(args.manifest_dir)
    task_ids = asyncio.run(_select_task_ids(provider, args))
    if not task_ids:
        raise SystemExit(
            "No task ids selected. Pass --task-id or --diverse-batch-size."
        )

    worker_count = min(args.worker_concurrency, len(task_ids))
    wave_plan = asyncio.run(_plan_task_waves(provider, task_ids, worker_count, args))
    if args.plan_only:
        _write_plan_only_report(
            provider=provider,
            task_ids=task_ids,
            worker_count=worker_count,
            wave_plan=wave_plan,
            output_path=output_path,
            similarity_threshold=args.max_task_similarity,
        )
        return
    visual_capture_setup = _prepare_visual_capture(
        args=args,
        output_path=output_path,
        worker_count=worker_count,
    )
    database_url = _database_url(args, timestamp, worker_count)
    _validate_parallel_database(args, worker_count, database_url)
    checkpoint_payload = _load_checkpoint_payload(args.checkpoint_path, args.resume)
    resume_state = _resume_state_from_checkpoint(
        checkpoint_payload,
        task_ids=task_ids,
        task_waves=wave_plan.waves,
        database_url=database_url,
    )
    checkpoint_job_id = (
        str(checkpoint_payload.get("job_id"))
        if isinstance(checkpoint_payload, dict) and checkpoint_payload.get("job_id")
        else None
    )
    if args.job_id and checkpoint_job_id and args.job_id != checkpoint_job_id:
        raise SystemExit("--job-id does not match the resumed checkpoint job_id.")
    job_id = args.job_id or checkpoint_job_id or f"week10_live_{timestamp}"
    checkpoint_callback = _checkpoint_callback(
        path=args.checkpoint_path,
        job_id=job_id,
        task_ids=task_ids,
        task_waves=wave_plan.waves,
        database_url=database_url,
    )
    server_pool, server_pool_state = _resolve_server_pool(args, worker_count)
    _validate_parallel_placement(args, worker_count, server_pool)
    session_factory = _create_database(database_url)
    _wait_for_server_pool(server_pool, needs_rcon=_needs_rcon(args))

    worker_supervisors: dict[str, WorkerProcessSupervisor] = {}
    worker_specs: list[LiveWorkerSpec] = []
    visual_readiness_event = asyncio.Event()
    if not args.spectator_player:
        visual_readiness_event.set()
    try:
        for index in range(worker_count):
            server = server_pool[index] if len(server_pool) > 1 else server_pool[0]
            worker_id = f"worker-{index + 1}"
            port = _free_port()
            supervisor = WorkerProcessSupervisor(
                worker_id=worker_id,
                port=port,
                log_path=output_path.parent / "worker_logs" / f"{worker_id}.log",
            )
            supervisor.start()
            worker_supervisors[worker_id] = supervisor
            _wait_for_tcp("127.0.0.1", port, timeout_sec=20)
            worker_specs.append(
                LiveWorkerSpec(
                    worker_id=worker_id,
                    worker_url=f"ws://127.0.0.1:{port}",
                    username=f"{args.username_prefix}{index + 1}",
                    server_id=server.server_id,
                    minecraft_host=server.host,
                    minecraft_port=server.server_port,
                    rcon_host=args.rcon_host or server.host,
                    rcon_port=server.rcon_port,
                    world_dir=server.world_dir,
                )
            )

        rcon_executors = _worker_rcon_executors(args, worker_specs)
        primary_rcon_executor = (
            rcon_executors.get(worker_specs[0].worker_id) if worker_specs else None
        )
        model_router_factory = (
            _scripted_router_factory
            if args.scripted
            else _resilient_model_router_factory(args)
        )

        run_event_stream = (
            LiveRunEventStream(worker_specs[0].username)
            if args.spectator_player and worker_specs
            else None
        )
        spectator_audit_callback = (
            _build_spectator_audit_callback(
                session_factory=session_factory,
                agent_id=worker_specs[0].username,
                worker_id=worker_specs[0].worker_id,
            )
            if run_event_stream is not None
            else None
        )
        runner = LiveTrainingRunner(
            task_provider=provider,
            minecraft=LiveMinecraftConfig(
                host=server_pool[0].host,
                port=server_pool[0].server_port,
                username_prefix=args.username_prefix,
                spawn_timeout_ms=args.spawn_timeout_ms,
            ),
            workers=worker_specs,
            session_factory=session_factory,
            config=LiveTrainingConfig(
                job_id=job_id,
                runtime_profile="live-mineflayer-training-scripted"
                if args.scripted
                else "live-mineflayer-training",
                model_profile="scripted-week10-live"
                if args.scripted
                else settings.model_default,
                budget=TrainingBudget(
                    max_steps_per_task=args.max_steps_per_task,
                    max_runtime_sec_per_task=args.max_runtime_sec_per_task,
                    worker_concurrency=worker_count,
                ),
                duplicate_threshold=args.duplicate_threshold,
                auto_promote=args.auto_promote,
                max_retrieved_skills=args.max_retrieved_skills,
                min_skill_relevance=args.min_skill_relevance,
                model_timeout_retries=args.model_timeout_retries,
                model_timeout_backoff_sec=tuple(
                    args.model_timeout_backoff_sec or (2.0, 5.0)
                ),
                model_timeout_requeues=args.model_timeout_requeues,
                model_timeout_requeue_delay_sec=args.model_timeout_requeue_delay_sec,
                worker_failure_requeues=args.worker_failure_requeues,
                max_task_retries=args.max_task_retries,
                task_retry_delay_sec=args.task_retry_delay_sec,
                task_waves=tuple(tuple(wave) for wave in wave_plan.waves),
                start_delay_sec=args.start_delay_sec,
                clear_inventory_on_reset=bool(
                    args.clear_inventory_on_reset or args.clear_all_inventory_on_reset
                ),
                clear_inventory_items=tuple(args.clear_item or ()),
                clear_all_inventory_on_reset=bool(args.clear_all_inventory_on_reset),
                clear_inventory_wait_ms=args.clear_inventory_wait_ms,
                reset_drop_fallback=not args.no_reset_drop_fallback,
                server_command_reset=ServerCommandResetConfig(
                    enabled=bool(args.rcon_reset or args.rcon_random_teleport_on_reset),
                    clear_inventory=True,
                    clear_dropped_items=not args.rcon_no_clear_drops,
                    restore_player_state=not args.rcon_no_restore_player_state,
                    align_biome=bool(args.rcon_align_biome),
                    set_time=args.rcon_set_time,
                    set_weather=args.rcon_set_weather,
                ),
                random_teleport_reset=RandomTeleportResetConfig(
                    enabled=bool(
                        args.rcon_random_teleport_on_reset
                        or args.rcon_random_teleport_when_biome_missing
                    ),
                    center_x=args.rcon_random_teleport_center_x,
                    center_z=args.rcon_random_teleport_center_z,
                    spread_distance=args.rcon_random_teleport_spread_distance,
                    max_range=args.rcon_random_teleport_max_range,
                    clear_start_position=not args.rcon_random_teleport_keep_start_position,
                    only_when_biome_missing=bool(
                        args.rcon_random_teleport_when_biome_missing
                        and not args.rcon_random_teleport_on_reset
                    ),
                ),
                threat_pause=ThreatPauseConfig(
                    enabled=bool(args.threat_pause),
                    threat_distance=args.threat_pause_distance,
                ),
                initial_visual_snapshot=bool(args.initial_visual_snapshot),
            ),
            model_router_factory=model_router_factory,
            runtime_factory=(
                _visual_runtime_factory(
                    visual_capture_setup.provider,
                    visual_readiness_event,
                    visual_snapshots=bool(args.agent_visual_snapshots),
                    progress_policy=_mineclip_progress_policy(args),
                    progress_scorer_url=args.mineclip_progress_scorer_url,
                    progress_scorer_timeout_sec=args.mineclip_progress_scorer_timeout_sec,
                )
                if (args.agent_visual_snapshots or args.mineclip_progress_feedback)
                and visual_capture_setup.provider is not None
                else None
            ),
            server_command_executors=rcon_executors,
            resume_state=resume_state,
            wave_checkpoint_callback=checkpoint_callback,
            event_callback=run_event_stream.publish
            if run_event_stream is not None
            else None,
            worker_recovery_callback=_worker_recovery_callback(worker_supervisors),
        )
        try:
            report, recording_payload = asyncio.run(
                _run_training_with_optional_recording(
                    runner=runner,
                    task_ids=task_ids,
                    args=args,
                    worker_specs=worker_specs,
                    rcon_executor=primary_rcon_executor,
                    recording_output_path=recording_output_path,
                    visual_capture_setup=visual_capture_setup,
                    run_event_stream=run_event_stream,
                    spectator_audit_callback=spectator_audit_callback,
                    visual_readiness_event=visual_readiness_event,
                )
            )
        except KeyboardInterrupt:
            asyncio.run(
                _mark_job_runs_interrupted(
                    session_factory=session_factory,
                    job_id=job_id,
                    reason="keyboard_interrupt",
                )
            )
            raise SystemExit(130) from None
        payload = {
            **report.to_json(),
            "database_url": database_url,
            "output_path": str(output_path),
            "task_ids": task_ids,
            "workers": [as_json(worker) for worker in worker_specs],
            "worker_processes": [
                worker_supervisors[worker.worker_id].to_json()
                for worker in worker_specs
            ],
            "server_pool": _server_pool_payload(
                server_pool,
                worker_specs,
                state_path=args.server_pool_state,
                state_payload=server_pool_state,
            ),
            "model_execution": {
                "max_concurrency": 0 if args.scripted else args.model_concurrency,
                "provider_transient_retries": 0
                if args.scripted
                else args.provider_transient_retries,
                "max_retrieved_skills": args.max_retrieved_skills,
                "min_skill_relevance": args.min_skill_relevance,
            },
            "mineclip_progress_feedback": {
                "enabled": bool(args.mineclip_progress_feedback),
                "scorer_url": args.mineclip_progress_scorer_url
                if args.mineclip_progress_feedback
                else None,
                "important_actions": list(
                    _mineclip_progress_policy(args).important_actions
                    if _mineclip_progress_policy(args) is not None
                    else ()
                ),
                "sample_fps": args.mineclip_progress_sample_fps,
                "post_action_frames": args.mineclip_progress_post_action_frames,
                "blocking": False,
                "success_authority": "human_review",
            },
            "checkpoint": {
                "path": str(args.checkpoint_path)
                if args.checkpoint_path is not None
                else None,
                "resumed": resume_state is not None,
                "resumed_completed_wave_count": resume_state.completed_wave_count
                if resume_state is not None
                else 0,
            },
            "scheduling": {
                "mode": "diverse_waves" if len(wave_plan.waves) > 1 else "shared_queue",
                "wave_count": len(wave_plan.waves),
                "waves": wave_plan.waves,
                "max_wave_similarity": wave_plan.max_wave_similarity,
                "wave_similarities": wave_plan.wave_similarities,
                "similarity_threshold": args.max_task_similarity,
                "threshold_violations": wave_plan.threshold_violations,
            },
            "recording": recording_payload,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        if args.checkpoint_path is not None:
            _write_checkpoint_payload(
                path=args.checkpoint_path,
                stage="complete",
                job_id=job_id,
                task_ids=task_ids,
                task_waves=wave_plan.waves,
                database_url=database_url,
                completed_wave_count=len(wave_plan.waves),
                attempt_outcomes=report.attempt_outcomes,
                skill_snapshot_revision=(report.skill_snapshot or {}).get("revision"),
                learning_snapshot_revision=(report.learning_snapshot or {}).get(
                    "revision"
                ),
            )
        print(json.dumps(_summary(payload), indent=2, sort_keys=True))
        if report.status == "completed_with_failures":
            raise SystemExit(1)
    finally:
        if not args.keep_workers:
            for supervisor in worker_supervisors.values():
                supervisor.stop()


async def _select_task_ids(
    provider: MineDojoTaskProvider,
    args: argparse.Namespace,
) -> list[str]:
    """Select explicit tasks or a diversity-aware batch from executable manifests."""

    if args.task_id and args.diverse_batch_size is None:
        return list(args.task_id)

    summaries = await provider.list_tasks()
    if args.task_id:
        requested_ids = set(args.task_id)
        summaries = [
            summary
            for summary in summaries
            if str(summary.get("task_id")) in requested_ids
        ]
    else:
        included_categories = set(args.category or ())
        excluded_categories = set(args.exclude_category or ())
        if included_categories:
            summaries = [
                summary
                for summary in summaries
                if str(summary.get("category")) in included_categories
            ]
        if excluded_categories:
            summaries = [
                summary
                for summary in summaries
                if str(summary.get("category")) not in excluded_categories
            ]
    candidate_ids = list(
        args.task_id or [str(summary["task_id"]) for summary in summaries]
    )
    if args.diverse_batch_size is None:
        return candidate_ids
    # Selecting the entire candidate set does not require the cubic greedy batch
    # ordering pass. The separate wave planner below still enforces low-similarity
    # placement inside each concurrent worker wave. This fast path is important
    # for full-catalog runs (currently 1,581 executable tasks).
    if args.diverse_batch_size == len(candidate_ids):
        return candidate_ids
    if args.stratified_batch:
        summaries_by_id = {
            str(summary["task_id"]): summary
            for summary in summaries
            if summary.get("task_id") is not None
        }
        quotas = _proportional_category_quotas(
            list(summaries_by_id.values()),
            args.diverse_batch_size,
        )
        selected_ids: list[str] = []
        for category in sorted(quotas):
            if quotas[category] == 0:
                continue
            category_ids = [
                task_id
                for task_id in candidate_ids
                if str(summaries_by_id.get(task_id, {}).get("category")) == category
            ]
            category_specs = [
                await provider.load_task(task_id) for task_id in category_ids
            ]
            category_selection = DiverseBatchPlanner().select_batch(
                category_specs,
                batch_size=quotas[category],
                max_pairwise_similarity=args.max_task_similarity,
            )
            selected_ids.extend(category_selection.selected_task_ids)
        return selected_ids
    task_specs = [await provider.load_task(task_id) for task_id in candidate_ids]
    selection = DiverseBatchPlanner().select_batch(
        task_specs,
        batch_size=args.diverse_batch_size,
        max_pairwise_similarity=args.max_task_similarity,
    )
    return selection.selected_task_ids


def _proportional_category_quotas(
    summaries: list[dict[str, Any]],
    batch_size: int,
) -> dict[str, int]:
    """Allocate a batch proportionally by category using deterministic largest remainders."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive.")
    counts: dict[str, int] = {}
    for summary in summaries:
        category = str(summary.get("category") or "unknown")
        counts[category] = counts.get(category, 0) + 1
    candidate_count = sum(counts.values())
    if batch_size > candidate_count:
        raise ValueError(
            f"Requested batch_size={batch_size} but only {candidate_count} tasks are available."
        )
    exact = {
        category: batch_size * count / candidate_count
        for category, count in counts.items()
    }
    quotas = {
        category: min(counts[category], int(exact[category])) for category in counts
    }
    remaining = batch_size - sum(quotas.values())
    ranked = sorted(
        counts,
        key=lambda category: (
            -(exact[category] - int(exact[category])),
            category,
        ),
    )
    while remaining > 0:
        allocated = False
        for category in ranked:
            if quotas[category] >= counts[category]:
                continue
            quotas[category] += 1
            remaining -= 1
            allocated = True
            if remaining == 0:
                break
        if not allocated:
            raise ValueError("Unable to allocate the requested stratified batch size.")
    return quotas


async def _plan_task_waves(
    provider: MineDojoTaskProvider,
    task_ids: list[str],
    worker_count: int,
    args: argparse.Namespace,
) -> DiverseWavePlan:
    """Arrange selected tasks into low-similarity concurrent waves."""

    if worker_count <= 1 or args.no_diverse_waves:
        return DiverseWavePlan(
            waves=[list(task_ids)],
            max_wave_similarity=0.0,
            wave_similarities=[0.0],
        )
    task_specs = [await provider.load_task(task_id) for task_id in task_ids]
    return DiverseWavePlanner().arrange(
        task_specs,
        wave_size=worker_count,
        max_pairwise_similarity=args.max_task_similarity,
    )


def _write_plan_only_report(
    *,
    provider: MineDojoTaskProvider,
    task_ids: list[str],
    worker_count: int,
    wave_plan: DiverseWavePlan,
    output_path: Path,
    similarity_threshold: float,
) -> None:
    """Persist a reproducible task/wave plan without touching live infrastructure."""

    task_specs = asyncio.run(_load_task_specs(provider, task_ids))
    category_counts: dict[str, int] = {}
    for task_spec in task_specs:
        category = str(task_spec.get("category") or "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1
    payload = {
        "mode": "plan_only",
        "created_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "task_count": len(task_ids),
        "worker_concurrency": worker_count,
        "category_counts": category_counts,
        "task_ids": task_ids,
        "waves": wave_plan.waves,
        "wave_count": len(wave_plan.waves),
        "max_wave_similarity": wave_plan.max_wave_similarity,
        "wave_similarities": wave_plan.wave_similarities,
        "similarity_threshold": similarity_threshold,
        "threshold_violations": wave_plan.threshold_violations,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                **payload,
                "task_ids": task_ids[:10],
                "waves": wave_plan.waves[:10],
                "wave_similarities": wave_plan.wave_similarities[:10],
                "console_preview_truncated": len(wave_plan.waves) > 10,
                "output_path": str(output_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


async def _load_task_specs(
    provider: MineDojoTaskProvider,
    task_ids: list[str],
) -> list[dict[str, Any]]:
    """Load selected task specs in their scheduled report order."""

    return [await provider.load_task(task_id) for task_id in task_ids]


def _load_checkpoint_payload(
    path: Path | None,
    resume: bool,
) -> dict[str, Any] | None:
    """Load a live-training checkpoint only when explicit resume was requested."""

    if not resume:
        return None
    if path is None:
        raise SystemExit("--resume requires --checkpoint-path.")
    if not path.is_file():
        raise SystemExit(f"Checkpoint was not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Checkpoint must contain a JSON object.")
    if payload.get("schema_version") != "week10-live-checkpoint.v1":
        raise SystemExit("Checkpoint schema_version is unsupported.")
    if payload.get("stage") == "complete":
        raise SystemExit("Checkpoint is already complete; no resume work remains.")
    return payload


def _resume_state_from_checkpoint(
    payload: dict[str, Any] | None,
    *,
    task_ids: list[str],
    task_waves: list[list[str]],
    database_url: str,
) -> LiveTrainingResumeState | None:
    """Validate a checkpoint plan and reconstruct completed attempt outcomes."""

    if payload is None:
        return None
    if payload.get("task_ids") != task_ids:
        raise SystemExit("Checkpoint task_ids do not match the newly selected batch.")
    if payload.get("task_waves") != task_waves:
        raise SystemExit(
            "Checkpoint task_waves do not match the newly selected schedule."
        )
    if payload.get("database_fingerprint") != _database_fingerprint(database_url):
        raise SystemExit(
            "Checkpoint database does not match the configured audit database."
        )
    raw_outcomes = payload.get("attempt_outcomes")
    if not isinstance(raw_outcomes, list):
        raise SystemExit("Checkpoint attempt_outcomes must be a list.")
    return LiveTrainingResumeState(
        completed_wave_count=int(payload.get("completed_wave_count", 0)),
        attempt_outcomes=tuple(
            _live_outcome_from_json(item)
            for item in raw_outcomes
            if isinstance(item, dict)
        ),
        skill_snapshot_revision=_optional_string(
            payload.get("skill_snapshot_revision")
        ),
        learning_snapshot_revision=_optional_string(
            payload.get("learning_snapshot_revision")
        ),
    )


def _checkpoint_callback(
    *,
    path: Path | None,
    job_id: str,
    task_ids: list[str],
    task_waves: list[list[str]],
    database_url: str,
) -> Callable[[int, list[LiveTrainingOutcome], str | None, str | None], Any] | None:
    """Build an async wave callback that atomically persists scheduler progress."""

    if path is None:
        return None

    async def callback(
        completed_wave_count: int,
        attempt_outcomes: list[LiveTrainingOutcome],
        skill_snapshot_revision: str | None,
        learning_snapshot_revision: str | None,
    ) -> None:
        """Persist one completed wave without exposing partial in-flight tasks."""

        _write_checkpoint_payload(
            path=path,
            stage="executing",
            job_id=job_id,
            task_ids=task_ids,
            task_waves=task_waves,
            database_url=database_url,
            completed_wave_count=completed_wave_count,
            attempt_outcomes=attempt_outcomes,
            skill_snapshot_revision=skill_snapshot_revision,
            learning_snapshot_revision=learning_snapshot_revision,
        )

    return callback


def _write_checkpoint_payload(
    *,
    path: Path,
    stage: str,
    job_id: str,
    task_ids: list[str],
    task_waves: list[list[str]],
    database_url: str,
    completed_wave_count: int,
    attempt_outcomes: list[LiveTrainingOutcome],
    skill_snapshot_revision: str | None,
    learning_snapshot_revision: str | None,
) -> None:
    """Atomically write one durable live-training checkpoint JSON object."""

    payload = {
        "schema_version": "week10-live-checkpoint.v1",
        "stage": stage,
        "updated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "job_id": job_id,
        "database_fingerprint": _database_fingerprint(database_url),
        "task_ids": task_ids,
        "task_waves": task_waves,
        "completed_wave_count": completed_wave_count,
        "skill_snapshot_revision": skill_snapshot_revision,
        "learning_snapshot_revision": learning_snapshot_revision,
        "attempt_outcomes": [asdict(outcome) for outcome in attempt_outcomes],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(path)


def _live_outcome_from_json(payload: dict[str, Any]) -> LiveTrainingOutcome:
    """Reconstruct one attempt outcome from a trusted local checkpoint payload."""

    skill_payload = payload.get("skill_update")
    learning_payload = payload.get("learning_update")
    usage_payload = payload.get("model_usage")
    return LiveTrainingOutcome(
        task_id=str(payload["task_id"]),
        attempt=int(payload["attempt"]),
        run_id=_optional_string(payload.get("run_id")),
        worker_id=str(payload["worker_id"]),
        username=str(payload["username"]),
        server_id=_optional_string(payload.get("server_id")),
        memory_namespace=str(payload["memory_namespace"]),
        success=bool(payload["success"]),
        status=str(payload["status"]),
        verifier=dict(payload.get("verifier") or {}),
        steps=int(payload.get("steps", 0)),
        duration_sec=float(payload.get("duration_sec", 0.0)),
        model_usage=LiveModelUsage(**dict(usage_payload or {})),
        runtime_error=_optional_string(payload.get("runtime_error")),
        failure_class=_optional_string(payload.get("failure_class")),
        skill_update=LiveSkillUpdate(**skill_payload)
        if isinstance(skill_payload, dict)
        else None,
        learning_update=LiveLearningUpdate(**learning_payload)
        if isinstance(learning_payload, dict)
        else None,
    )


def _optional_string(value: Any) -> str | None:
    """Return a non-empty string or None for checkpoint optional fields."""

    return str(value) if isinstance(value, str) and value else None


def _database_fingerprint(database_url: str) -> str:
    """Hash the audit database URL so resume can verify identity without storing secrets."""

    return hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]


def _scripted_router_factory(task_spec: dict[str, Any]) -> ModelRouter:
    """Build a scripted model router from manifest benchmark actions."""

    benchmark = (
        task_spec.get("benchmark")
        if isinstance(task_spec.get("benchmark"), dict)
        else {}
    )
    actions = list(benchmark.get("scripted_actions", []))
    model_id = "scripted-week10-live"
    return ModelRouter(
        default_model=model_id,
        provider=ScriptedActionProvider(actions),
        profiles={
            model_id: ModelProfile(id=model_id, provider="scripted", tool_json=True)
        },
    )


def _resilient_model_router_factory(
    args: argparse.Namespace,
) -> Callable[[dict[str, Any]], ModelRouter]:
    """Build routers that share one concurrency-limited remote model provider."""

    provider = ResilientModelProvider(
        OpenAICompatibleProvider(),
        max_concurrency=args.model_concurrency,
        transient_retries=args.provider_transient_retries,
        backoff_sec=tuple(args.provider_retry_backoff_sec or (2.0, 5.0)),
    )

    def factory(_task_spec: dict[str, Any]) -> ModelRouter:
        """Return one task-local router backed by the shared provider limiter."""

        return ModelRouter(provider=provider)

    return factory


def _visual_runtime_factory(
    frame_provider: MacOSWindowCaptureProvider,
    readiness_event: asyncio.Event,
    *,
    visual_snapshots: bool,
    progress_policy: CreativeProgressPolicy | None = None,
    progress_scorer_url: str | None = None,
    progress_scorer_timeout_sec: float = 30.0,
) -> Callable[[str, float], GameRuntime]:
    """Build Mineflayer runtimes with optional vision and asynchronous progress feedback."""

    def factory(worker_url: str, request_timeout: float) -> GameRuntime:
        """Wrap one worker client without changing its structured JSON-RPC transport."""

        runtime: GameRuntime = MineflayerClient(
            worker_url, request_timeout=request_timeout
        )
        if visual_snapshots:
            runtime = VisualSnapshotRuntime(
                runtime,
                frame_provider,
                readiness_event=readiness_event,
            )
        if progress_policy is not None:
            monitor = CreativeProgressMonitor(
                frame_provider,
                MineClipScorer(
                    progress_scorer_url or settings.mineclip_scorer_url,
                    timeout_sec=progress_scorer_timeout_sec,
                ),
                policy=progress_policy,
                readiness_event=readiness_event,
            )
            runtime = CreativeProgressFeedbackRuntime(runtime, monitor)
        return runtime

    return factory


def _validate_model_environment() -> None:
    """Fail fast when live LLM training has no configured Qwen-compatible endpoint."""

    if not settings.qwen_base_url:
        raise SystemExit("QWEN_BASE_URL is missing. Set it in .env or the environment.")
    if not settings.qwen_api_key:
        raise SystemExit("QWEN_API_KEY is missing. Set it in .env or the environment.")


def _validate_mineclip_progress_scorer(args: argparse.Namespace) -> None:
    """Fail before Minecraft startup when the requested online scorer is unavailable."""

    try:
        health = asyncio.run(
            MineClipScorer(
                args.mineclip_progress_scorer_url,
                timeout_sec=min(args.mineclip_progress_scorer_timeout_sec, 10.0),
            ).health()
        )
    except Exception as exc:  # noqa: BLE001 - convert scorer setup failures into one CLI error.
        raise SystemExit(
            "Online MineCLIP progress feedback was requested, but scorer preflight failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if health.get("status") != "ready":
        raise SystemExit(f"MineCLIP scorer is not ready: {health}")


def _needs_rcon(args: argparse.Namespace) -> bool:
    """Return whether any selected live feature requires server-authorized commands."""

    return bool(
        args.rcon_reset
        or args.threat_pause
        or args.spectator_player
        or args.rcon_random_teleport_on_reset
        or getattr(args, "rcon_random_teleport_when_biome_missing", False)
    )


def _worker_rcon_executors(
    args: argparse.Namespace,
    workers: list[LiveWorkerSpec],
) -> dict[str, RconServerCommandExecutor]:
    """Build one RCON executor for each worker's assigned server instance."""

    if not _needs_rcon(args):
        return {}
    password = args.rcon_password
    if not password:
        raise SystemExit(
            "MINECRAFT_RCON_PASSWORD or --rcon-password is required for --rcon-reset, "
            "--threat-pause, --rcon-random-teleport-on-reset, or --spectator-player."
        )
    executors: dict[str, RconServerCommandExecutor] = {}
    for worker in workers:
        if worker.rcon_port is None:
            raise SystemExit(
                f"Worker {worker.worker_id} server {worker.server_id} has no RCON port."
            )
        executors[worker.worker_id] = RconServerCommandExecutor(
            host=worker.rcon_host or worker.minecraft_host or args.host,
            port=worker.rcon_port,
            password=password,
            timeout_sec=args.rcon_timeout_sec,
        )
    return executors


def _validate_cli_limits(args: argparse.Namespace) -> None:
    """Reject unsafe or nonsensical live-training limits before starting processes."""

    if args.worker_concurrency <= 0:
        raise SystemExit("--worker-concurrency must be positive.")
    if args.max_task_retries < 0:
        raise SystemExit("--max-task-retries must be non-negative.")
    if args.max_retrieved_skills < 0:
        raise SystemExit("--max-retrieved-skills must be non-negative.")
    if not 0.0 <= args.min_skill_relevance <= 1.0:
        raise SystemExit("--min-skill-relevance must be between 0 and 1.")
    if args.task_retry_delay_sec < 0:
        raise SystemExit("--task-retry-delay-sec must be non-negative.")
    if args.worker_failure_requeues < 0:
        raise SystemExit("--worker-failure-requeues must be non-negative.")
    if args.model_concurrency <= 0:
        raise SystemExit("--model-concurrency must be positive.")
    if args.provider_transient_retries < 0:
        raise SystemExit("--provider-transient-retries must be non-negative.")
    if args.spectator_chunk_sync_delay_sec < 0:
        raise SystemExit("--spectator-chunk-sync-delay-sec must be non-negative.")
    if args.spectator_rebind_interval_sec <= 0:
        raise SystemExit("--spectator-rebind-interval-sec must be positive.")
    if args.spectator_full_sync_interval_sec < 0:
        raise SystemExit("--spectator-full-sync-interval-sec must be non-negative.")
    if args.spectator_resync_distance_blocks < 0:
        raise SystemExit("--spectator-resync-distance-blocks must be non-negative.")
    if args.spectator_resync_cooldown_sec < 0:
        raise SystemExit("--spectator-resync-cooldown-sec must be non-negative.")
    if args.agent_visual_snapshots and not args.recording_window_title:
        raise SystemExit(
            "--agent-visual-snapshots requires --recording-window-title so captures cannot "
            "silently target another application."
        )
    if args.mineclip_progress_feedback and not args.recording_window_title:
        raise SystemExit(
            "--mineclip-progress-feedback requires --recording-window-title so online scoring "
            "cannot capture another application."
        )
    if args.mineclip_progress_sample_fps <= 0:
        raise SystemExit("--mineclip-progress-sample-fps must be positive.")
    if args.mineclip_progress_min_interval_sec < 0:
        raise SystemExit("--mineclip-progress-min-interval-sec must be non-negative.")
    if args.mineclip_progress_post_action_frames < 0:
        raise SystemExit("--mineclip-progress-post-action-frames must be non-negative.")
    if args.mineclip_progress_scorer_timeout_sec <= 0:
        raise SystemExit("--mineclip-progress-scorer-timeout-sec must be positive.")
    if args.initial_visual_snapshot and not args.agent_visual_snapshots:
        raise SystemExit("--initial-visual-snapshot requires --agent-visual-snapshots.")


def _mineclip_progress_policy(
    args: argparse.Namespace,
) -> CreativeProgressPolicy | None:
    """Build a bounded online MineCLIP policy only when explicitly enabled."""

    if not args.mineclip_progress_feedback:
        return None
    important_actions = tuple(
        args.mineclip_progress_action or CreativeProgressPolicy().important_actions
    )
    return CreativeProgressPolicy(
        important_actions=important_actions,
        sample_fps=args.mineclip_progress_sample_fps,
        post_action_frames=args.mineclip_progress_post_action_frames,
        min_checkpoint_interval_sec=args.mineclip_progress_min_interval_sec,
    )


def _resolve_server_pool(
    args: argparse.Namespace,
    worker_count: int,
) -> tuple[list[MinecraftServerInstanceSpec], dict[str, Any] | None]:
    """Resolve isolated pool placements or a single explicit development server."""

    if args.server_pool_state is not None:
        servers, state = load_server_pool_state(args.server_pool_state)
        if worker_count > len(servers):
            raise SystemExit(
                f"Requested {worker_count} workers but pool contains only {len(servers)} servers."
            )
        selected = servers[:worker_count]
        if any(server.max_workers != 1 for server in selected):
            raise SystemExit(
                "Isolated live training requires max_workers=1 for each server."
            )
        return selected, state

    uses_rcon = _needs_rcon(args)
    server = MinecraftServerInstanceSpec(
        server_id="server-1",
        host=args.host,
        server_port=args.port,
        rcon_port=args.rcon_port if uses_rcon else None,
        world_dir="external_or_lan_world",
        heap_gb=3.0,
        max_workers=worker_count,
    )
    return [server], None


def _validate_parallel_placement(
    args: argparse.Namespace,
    worker_count: int,
    server_pool: list[MinecraftServerInstanceSpec],
) -> None:
    """Prevent accidental multi-worker training in a shared mutable world."""

    if worker_count <= 1:
        return
    if len(server_pool) < worker_count and not args.allow_shared_server_workers:
        raise SystemExit(
            "Parallel live training requires one isolated Minecraft server per worker. "
            "Pass --server-pool-state or explicitly opt into the development-only "
            "--allow-shared-server-workers mode."
        )


def _database_url(args: argparse.Namespace, timestamp: str, worker_count: int) -> str:
    """Select PostgreSQL for formal parallel runs and SQLite for local single-worker runs."""

    if args.database_url and args.database_path:
        raise SystemExit("Pass only one of --database-url and --database-path.")
    if args.database_url:
        return str(args.database_url)
    if args.database_path:
        return f"sqlite+pysqlite:///{args.database_path.as_posix()}"
    if worker_count > 1 and not args.scripted and not args.allow_sqlite_parallel:
        return settings.database_url
    database_path = ROOT / "runs" / f"week10_live_training_{timestamp}.sqlite3"
    return f"sqlite+pysqlite:///{database_path.as_posix()}"


def _validate_parallel_database(
    args: argparse.Namespace,
    worker_count: int,
    database_url: str,
) -> None:
    """Require a concurrent database for formal multi-worker LLM training."""

    if (
        worker_count > 1
        and not args.scripted
        and database_url.startswith("sqlite")
        and not args.allow_sqlite_parallel
    ):
        raise SystemExit(
            "Formal multi-worker training requires PostgreSQL. Start Docker services and pass "
            "--database-url, or use --allow-sqlite-parallel only for a development smoke test."
        )


def _wait_for_server_pool(
    server_pool: list[MinecraftServerInstanceSpec],
    *,
    needs_rcon: bool,
) -> None:
    """Fail fast unless every assigned game and required RCON endpoint is reachable."""

    for server in server_pool:
        _wait_for_tcp(server.host, server.server_port, timeout_sec=20)
        if needs_rcon:
            if server.rcon_port is None:
                raise SystemExit(f"Server {server.server_id} is missing an RCON port.")
            _wait_for_tcp(server.host, server.rcon_port, timeout_sec=20)


def _create_database(database_url: str) -> sessionmaker[Session]:
    """Create audit tables for live training."""

    if database_url.startswith("sqlite+pysqlite:///"):
        sqlite_path = Path(database_url.removeprefix("sqlite+pysqlite:///"))
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    )
    engine = create_engine(database_url, future=True, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _recording_output_path(args: argparse.Namespace, output_path: Path) -> Path | None:
    """Return the MP4 path used by optional screen recording."""

    if not args.record_agent_video:
        return None
    if args.recording_output is not None:
        return args.recording_output
    return output_path.with_suffix(".mp4")


def _prepare_visual_capture(
    *,
    args: argparse.Namespace,
    output_path: Path,
    worker_count: int,
) -> VisualCaptureSetup:
    """Preflight the real Minecraft window before starting workers or remote model calls."""

    needs_trusted_window = bool(
        getattr(args, "agent_visual_snapshots", False)
        or getattr(args, "mineclip_progress_feedback", False)
        or (args.record_agent_video and args.recording_window_title)
    )
    if not needs_trusted_window:
        return VisualCaptureSetup(
            provider=None,
            preflight=None,
            video_filter=args.recording_filter,
            artifact_dir=None,
        )
    if worker_count != 1 and getattr(args, "agent_visual_snapshots", False):
        raise SystemExit(
            "On-demand client vision currently supports one worker per visible Minecraft camera."
        )
    if worker_count != 1 and getattr(args, "mineclip_progress_feedback", False):
        raise SystemExit(
            "Online MineCLIP feedback currently supports one worker per visible Minecraft camera."
        )
    visual_dir_arg = getattr(args, "visual_snapshot_dir", None)
    visual_dir = (
        visual_dir_arg.expanduser().resolve()
        if isinstance(visual_dir_arg, Path)
        else (output_path.parent / "visual_snapshots").resolve()
    )
    provider = MacOSWindowCaptureProvider(
        title=str(args.recording_window_title),
        owner=getattr(args, "recording_window_owner", None),
        output_dir=visual_dir,
    )
    try:
        preflight = provider.preflight()
    except MacOSWindowCaptureError as exc:
        raise SystemExit(
            f"Minecraft window preflight failed before live execution: {exc}"
        ) from exc
    target = provider.target
    assert target is not None
    expected_filter = crop_filter_for_window(
        target,
        args.recording_window_scale,
    )
    video_filter = args.recording_filter or expected_filter
    filter_matches_window = args.recording_filter in {None, expected_filter}
    return VisualCaptureSetup(
        provider=provider,
        preflight={
            "matched": True,
            "trusted_minecraft_window": filter_matches_window,
            "title": args.recording_window_title,
            "owner_constraint": getattr(args, "recording_window_owner", None),
            "window": target.to_json(),
            "frame": preflight,
            "filter": video_filter,
            "expected_filter": expected_filter,
            "filter_source": "explicit" if args.recording_filter else "window_bounds",
            "filter_matches_window": filter_matches_window,
            "trust_failure_reason": None
            if filter_matches_window
            else "explicit_recording_filter_does_not_match_window_bounds",
        },
        video_filter=video_filter,
        artifact_dir=str(visual_dir),
    )


def _recording_window_filter(
    *,
    title: str,
    scale: float,
    owner: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Build a crop only from a trusted layer-0 Minecraft window identity."""

    if sys.platform != "darwin":
        return None, {
            "matched": False,
            "title": title,
            "error": "window_crop_only_supported_on_macos",
        }
    try:
        target = select_macos_window(title=title, owner=owner)
        crop = crop_filter_for_window(target, scale)
    except (MacOSWindowCaptureError, ValueError) as exc:
        return None, {
            "matched": False,
            "title": title,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return crop, {
        "matched": True,
        "trusted_minecraft_window": True,
        "title": title,
        "owner_constraint": owner,
        "window": target.to_json(),
        "scale": scale,
        "filter": crop,
    }


def _visible_macos_windows() -> list[dict[str, Any]]:
    """Return visible CoreGraphics windows for diagnostics and compatibility tests."""

    return [window.to_json() for window in visible_macos_windows()]


def _crop_filter_from_bounds(bounds: dict[str, Any], scale: float) -> str | None:
    """Convert AppleScript window bounds into an even-pixel ffmpeg crop filter."""

    try:
        x = max(0, _even_int(float(bounds["x"]) * scale))
        y = max(0, _even_int(float(bounds["y"]) * scale))
        width = max(2, _even_int(float(bounds["width"]) * scale))
        height = max(2, _even_int(float(bounds["height"]) * scale))
    except (KeyError, TypeError, ValueError):
        return None
    return f"crop={width}:{height}:{x}:{y}"


def _even_int(value: float) -> int:
    """Round a value down to the nearest even integer for yuv420p-compatible crop sizes."""

    rounded = int(round(value))
    return rounded if rounded % 2 == 0 else rounded - 1


def _build_spectator_audit_callback(
    *,
    session_factory: sessionmaker[Session],
    agent_id: str,
    worker_id: str,
) -> SpectatorAuditCallback:
    """Build a recorder callback that persists camera attempts against the active run."""

    recorder = PersistentEvaluationRecorder(
        session_factory,
        agent_id=agent_id,
        worker_id=worker_id,
    )

    async def callback(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Persist one spectator event without re-publishing it to the run event stream."""

        await recorder.record(run_id, event_type, payload)

    return callback


async def _mark_job_runs_interrupted(
    *,
    session_factory: sessionmaker[Session],
    job_id: str,
    reason: str,
) -> list[str]:
    """Finalize any still-running attempts if process-level interruption wins a cancellation race."""

    with session_factory() as session:
        active_runs = [
            (
                run.id,
                run.task_id,
                dict(run.task_spec or {}),
            )
            for run in session.scalars(
                select(RunRecord).where(RunRecord.status == "running")
            ).all()
            if run.id.startswith(f"{job_id}_")
        ]
    interrupted_run_ids: list[str] = []
    for run_id, task_id, task_spec in active_runs:
        training = (
            task_spec.get("training")
            if isinstance(task_spec.get("training"), dict)
            else {}
        )
        recorder = PersistentEvaluationRecorder(
            session_factory,
            task_id=task_id,
            agent_id=str(task_spec["agent_id"])
            if task_spec.get("agent_id") is not None
            else None,
            worker_id=str(training["worker_id"])
            if training.get("worker_id") is not None
            else None,
        )
        await recorder.record(
            run_id,
            "run_interrupted",
            {
                "task_id": task_id,
                "reason": reason,
                "source": "live_training_process_guard",
            },
        )
        interrupted_run_ids.append(run_id)
    return interrupted_run_ids


async def _run_training_with_optional_recording(
    *,
    runner: LiveTrainingRunner,
    task_ids: list[str],
    args: argparse.Namespace,
    worker_specs: list[LiveWorkerSpec],
    rcon_executor: RconServerCommandExecutor | None,
    recording_output_path: Path | None,
    visual_capture_setup: VisualCaptureSetup | None = None,
    run_event_stream: LiveRunEventStream | None = None,
    spectator_audit_callback: SpectatorAuditCallback | None = None,
    visual_readiness_event: asyncio.Event | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Run live training with optional spectator control and optional screen recording."""

    session = RecordingSession(enabled=bool(args.record_agent_video))
    session.spectator_player = args.spectator_player
    session.spectated_username = worker_specs[0].username if worker_specs else None
    session.agent_visual_snapshots_enabled = bool(
        getattr(args, "agent_visual_snapshots", False)
    )
    if visual_capture_setup is not None:
        session.visual_snapshot_dir = visual_capture_setup.artifact_dir
        if visual_capture_setup.preflight is not None:
            session.window_capture = dict(visual_capture_setup.preflight)
    recorder: AgentScreenRecorder | None = None
    spectate_task: asyncio.Task[list[dict[str, Any]]] | None = None
    spectate_stop_event: asyncio.Event | None = None
    camera_ready_event: asyncio.Event | None = None
    restore_task: asyncio.Task[list[dict[str, Any]]] | None = None
    if args.record_agent_video:
        if recording_output_path is None:
            raise RuntimeError(
                "recording_output_path is required when recording is enabled."
            )
        session.output_path = str(recording_output_path)
        session.requested_window_title = args.recording_window_title
        video_filter = (
            visual_capture_setup.video_filter
            if visual_capture_setup is not None
            else args.recording_filter
        )
        if (
            session.window_capture is None
            and args.recording_window_title
            and not video_filter
        ):
            video_filter, session.window_capture = _recording_window_filter(
                title=args.recording_window_title,
                scale=args.recording_window_scale,
                owner=getattr(args, "recording_window_owner", None),
            )
        session.effective_recording_filter = video_filter
        recorder = AgentScreenRecorder(
            output_path=recording_output_path,
            input_spec=args.recording_input,
            fps=args.recording_fps,
            video_size=args.recording_video_size,
            video_filter=video_filter,
        )
    if len(worker_specs) > 1 and (args.record_agent_video or args.spectator_player):
        session.note = (
            "Multiple workers are running; spectator/recording follows the first worker only. "
            "Use worker-concurrency=1 for a clean single-agent view."
        )
    if args.spectator_player and session.spectated_username:
        if rcon_executor is None:
            session.error = (
                "RCON executor is required for spectator follow but is unavailable."
            )
        elif run_event_stream is None:
            session.error = "A persisted run event stream is required so spectator follow starts after reset."
        else:
            spectate_stop_event = asyncio.Event()
            camera_ready_event = asyncio.Event()
            spectate_task = asyncio.create_task(
                _spectate_agent(
                    executor=rcon_executor,
                    spectator_player=args.spectator_player,
                    target_username=session.spectated_username,
                    retries=args.recording_spectate_retries,
                    interval_sec=args.recording_spectate_interval_sec,
                    chunk_sync_delay_sec=getattr(
                        args,
                        "spectator_chunk_sync_delay_sec",
                        0.75,
                    ),
                    rebind_interval_sec=getattr(
                        args, "spectator_rebind_interval_sec", 10.0
                    ),
                    full_sync_interval_sec=getattr(
                        args, "spectator_full_sync_interval_sec", 0.0
                    ),
                    resync_distance_blocks=getattr(
                        args, "spectator_resync_distance_blocks", 96.0
                    ),
                    resync_cooldown_sec=getattr(
                        args, "spectator_resync_cooldown_sec", 30.0
                    ),
                    stop_event=spectate_stop_event,
                    run_event_stream=run_event_stream,
                    audit_callback=spectator_audit_callback,
                    camera_ready_event=camera_ready_event,
                )
            )
    if (
        args.spectator_player
        and camera_ready_event is None
        and visual_readiness_event is not None
    ):
        visual_readiness_event.set()
    runner_task: asyncio.Task[Any] | None = None
    try:
        if camera_ready_event is None and recorder is not None:
            _start_screen_recorder(recorder, session)
        runner_task = asyncio.create_task(runner.run(task_ids))
        if camera_ready_event is not None and (
            recorder is not None or session.agent_visual_snapshots_enabled
        ):
            camera_ready = await _wait_for_camera_readiness(
                camera_ready_event,
                runner_task,
                timeout_sec=30.0,
            )
            session.camera_ready_before_recording = camera_ready
            if camera_ready and recorder is not None:
                _start_screen_recorder(recorder, session)
            elif recorder is not None:
                session.error = (
                    session.error or "Spectator camera was not ready before recording."
                )
            if visual_readiness_event is not None:
                visual_readiness_event.set()
        elif visual_readiness_event is not None:
            visual_readiness_event.set()
        report = await runner_task
    finally:
        if visual_readiness_event is not None:
            visual_readiness_event.set()
        if spectate_stop_event is not None:
            spectate_stop_event.set()
        if spectate_task is not None:
            session.spectate_attempts = await _finish_background_spectate(
                spectate_task,
                timeout_sec=2.0,
                cancel_on_timeout=True,
            )
        if (
            visual_capture_setup is not None
            and visual_capture_setup.provider is not None
        ):
            try:
                postflight = await asyncio.to_thread(
                    visual_capture_setup.provider.capture_sync,
                    label="postflight",
                    refresh_target=True,
                )
            except Exception as exc:  # noqa: BLE001 - retain capture diagnostics in audit.
                postflight = {
                    "available": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            if session.window_capture is None:
                session.window_capture = {}
            session.window_capture["postflight"] = postflight
        if (
            args.spectator_player
            and rcon_executor is not None
            and not args.recording_no_restore_spectator
        ):
            restore_task = asyncio.create_task(
                _restore_spectator_player(
                    executor=rcon_executor,
                    spectator_player=args.spectator_player,
                )
            )
            session.restore_attempts = await _finish_background_spectate(
                restore_task,
                timeout_sec=5.0,
                cancel_on_timeout=False,
            )
        if recorder is not None:
            try:
                session.ffmpeg_returncode = recorder.stop()
            except Exception as exc:  # noqa: BLE001 - preserve training result even if cleanup fails.
                session.error = f"{type(exc).__name__}: {exc}"
        if args.record_agent_video and recording_output_path is not None:
            session.validation = _recording_validation_payload(
                session=session,
                recording_output_path=recording_output_path,
            )
    return report, session.to_json()


def _start_screen_recorder(
    recorder: AgentScreenRecorder,
    session: RecordingSession,
) -> None:
    """Start ffmpeg once and retain any startup failure in the recording audit payload."""

    if session.ffmpeg_started:
        return
    try:
        recorder.start()
    except Exception as exc:  # noqa: BLE001 - recording failure must not hide run evidence.
        session.error = f"{type(exc).__name__}: {exc}"
        return
    session.ffmpeg_command = list(recorder.command)
    session.ffmpeg_started = True
    session.recording_started_at = datetime.now(tz=UTC).isoformat()


async def _wait_for_camera_readiness(
    camera_ready_event: asyncio.Event,
    runner_task: asyncio.Task[Any],
    *,
    timeout_sec: float,
) -> bool:
    """Wait for spectator attachment without delaying cleanup after an early run exit."""

    ready_task = asyncio.create_task(camera_ready_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {ready_task, runner_task},
            timeout=max(0.1, timeout_sec),
            return_when=asyncio.FIRST_COMPLETED,
        )
        return ready_task in done and camera_ready_event.is_set()
    finally:
        if not ready_task.done():
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)


def _recording_validation_payload(
    *,
    session: RecordingSession,
    recording_output_path: Path,
) -> dict[str, Any]:
    """Combine ffmpeg, video probe, and trusted-window evidence into one gate."""

    video = validate_video_artifact(recording_output_path).to_json()
    reasons = list(video.get("reasons") or [])
    if not session.ffmpeg_started:
        reasons.append("ffmpeg_not_started")
    if session.ffmpeg_returncode not in {0, None}:
        reasons.append("ffmpeg_failed")
    if session.error:
        reasons.append("recording_error")
    window_capture = (
        session.window_capture if isinstance(session.window_capture, dict) else {}
    )
    trusted_window = bool(window_capture.get("trusted_minecraft_window"))
    if session.requested_window_title and not trusted_window:
        reasons.append("minecraft_window_not_trusted")
        if window_capture.get("trust_failure_reason"):
            reasons.append(str(window_capture["trust_failure_reason"]))
    postflight = window_capture.get("postflight")
    if trusted_window and (
        not isinstance(postflight, dict) or postflight.get("available") is not True
    ):
        reasons.append("minecraft_window_postflight_failed")
    preflight_window = window_capture.get("window")
    postflight_window = (
        postflight.get("window") if isinstance(postflight, dict) else None
    )
    if isinstance(preflight_window, dict) and isinstance(postflight_window, dict):
        if preflight_window.get("window_id") != postflight_window.get("window_id"):
            reasons.append("minecraft_window_identity_changed")
        elif _window_bounds_changed(
            preflight_window.get("bounds"),
            postflight_window.get("bounds"),
        ):
            reasons.append("minecraft_window_bounds_changed")
    unique_reasons = list(dict.fromkeys(str(reason) for reason in reasons))
    return {
        "valid": bool(video.get("valid")) and not unique_reasons,
        "trusted_minecraft_window": trusted_window,
        "reasons": unique_reasons,
        "video": video,
        "preflight": window_capture.get("frame"),
        "postflight": postflight,
    }


def _window_bounds_changed(before: Any, after: Any, *, tolerance: float = 2.0) -> bool:
    """Detect movement or resize that would invalidate a static screen-crop recording."""

    if not isinstance(before, dict) or not isinstance(after, dict):
        return True
    for key in ("x", "y", "width", "height"):
        try:
            if abs(float(before[key]) - float(after[key])) > tolerance:
                return True
        except (KeyError, TypeError, ValueError):
            return True
    return False


async def _spectate_agent(
    *,
    executor: RconServerCommandExecutor,
    spectator_player: str,
    target_username: str,
    retries: int,
    interval_sec: float,
    chunk_sync_delay_sec: float,
    rebind_interval_sec: float = 10.0,
    full_sync_interval_sec: float = 0.0,
    resync_distance_blocks: float = 96.0,
    resync_cooldown_sec: float = 30.0,
    stop_event: asyncio.Event | None = None,
    run_event_stream: LiveRunEventStream,
    audit_callback: SpectatorAuditCallback | None = None,
    camera_ready_event: asyncio.Event | None = None,
) -> list[dict[str, Any]]:
    """Attach after reset and repair the camera only when live state indicates drift."""

    attempts: list[dict[str, Any]] = []
    run_stop_event = stop_event or asyncio.Event()
    attempt_index = 0
    retry_limit = max(1, retries)
    consecutive_failures = 0
    current_run: LiveRunStartedEvent | None = None
    attached = False
    last_sync_attempt_at: float | None = None
    last_successful_sync_at: float | None = None
    last_synced_state: LiveAgentCameraState | None = None
    loop = asyncio.get_running_loop()
    while not run_stop_event.is_set():
        timeout_sec: float | None = None
        if current_run is not None:
            timeout_sec = (
                max(0.1, rebind_interval_sec)
                if attached or consecutive_failures >= retry_limit
                else max(0.1, interval_sec)
            )
        run_started, stopped = await _wait_for_run_event_or_stop(
            run_event_stream=run_event_stream,
            stop_event=run_stop_event,
            timeout_sec=timeout_sec,
        )
        if stopped:
            break
        reset_spectator_mode = False
        trigger: str | None = None
        phase: str | None = None
        distance_probe: dict[str, Any] | None = None
        current_state: LiveAgentCameraState | None = None
        if run_started is not None:
            current_run = run_started
            current_state = run_event_stream.latest_camera_state(current_run.run_id)
            attached = False
            consecutive_failures = 0
            last_sync_attempt_at = None
            last_successful_sync_at = None
            last_synced_state = None
            reset_spectator_mode = True
            trigger = "post_reset_run_started"
            phase = "post_reset_sync"
        elif current_run is None:
            continue
        elif not attached:
            reset_spectator_mode = True
            trigger = "retry_after_failure"
            phase = "post_reset_retry"
        else:
            current_state = run_event_stream.latest_camera_state(current_run.run_id)
            state_trigger = _camera_state_change_trigger(
                last_synced_state,
                current_state,
            )
            if state_trigger is not None:
                trigger = state_trigger
                phase = "target_state_resync"
            else:
                distance_probe = await _probe_spectator_distance(
                    executor=executor,
                    spectator_player=spectator_player,
                    target_username=target_username,
                )
                if run_stop_event.is_set():
                    break
                now = loop.time()
                cooldown_elapsed = (
                    last_sync_attempt_at is None
                    or now - last_sync_attempt_at >= max(0.0, resync_cooldown_sec)
                )
                distance = distance_probe.get("horizontal_distance_blocks")
                if (
                    resync_distance_blocks > 0
                    and isinstance(distance, (int, float))
                    and distance >= resync_distance_blocks
                    and cooldown_elapsed
                ):
                    trigger = "spectator_distance_exceeded"
                    phase = "distance_resync"
                elif (
                    full_sync_interval_sec > 0
                    and last_successful_sync_at is not None
                    and now - last_successful_sync_at >= full_sync_interval_sec
                    and cooldown_elapsed
                ):
                    trigger = "periodic_chunk_sync"
                    phase = "periodic_full_sync"
                else:
                    if current_state is not None:
                        last_synced_state = _merge_camera_state(
                            last_synced_state,
                            current_state,
                        )
                    continue

        if trigger is None or phase is None:
            continue
        if run_stop_event.is_set():
            break
        attempt_index += 1
        last_sync_attempt_at = loop.time()
        payload = await _synchronize_spectator_camera(
            executor=executor,
            spectator_player=spectator_player,
            target_username=target_username,
            chunk_sync_delay_sec=max(0.0, chunk_sync_delay_sec),
            stop_event=run_stop_event,
            reset_spectator_mode=reset_spectator_mode,
        )
        if current_state is None:
            current_state = run_event_stream.latest_camera_state(current_run.run_id)
        payload.update(
            {
                "run_id": current_run.run_id,
                "task_id": current_run.task_id,
                "agent_id": current_run.agent_id,
                "worker_id": current_run.worker_id,
                "spectator_player": spectator_player,
                "target_username": target_username,
                "attempt": attempt_index,
                "phase": phase,
                "trigger": trigger,
                "camera_state": (
                    current_state.to_json() if current_state is not None else None
                ),
                "distance_probe": distance_probe,
            }
        )
        await _audit_spectator_attempt(payload, audit_callback)
        attempts.append(payload)
        succeeded = bool(payload.get("success"))
        if succeeded:
            attached = True
            consecutive_failures = 0
            last_successful_sync_at = loop.time()
            if current_state is not None:
                last_synced_state = _merge_camera_state(
                    last_synced_state,
                    current_state,
                )
            if camera_ready_event is not None and phase in {
                "post_reset_sync",
                "post_reset_retry",
            }:
                camera_ready_event.set()
        else:
            attached = False
            consecutive_failures += 1
    return attempts


async def _synchronize_spectator_camera(
    *,
    executor: RconServerCommandExecutor,
    spectator_player: str,
    target_username: str,
    chunk_sync_delay_sec: float,
    stop_event: asyncio.Event,
    reset_spectator_mode: bool = True,
) -> dict[str, Any]:
    """Move beside the target, allow chunk loading, and then attach the client camera."""

    preparation_commands = [f"/tp {spectator_player} {target_username}"]
    if reset_spectator_mode:
        preparation_commands = [
            f"/gamemode creative {spectator_player}",
            f"/gamemode spectator {spectator_player}",
            *preparation_commands,
        ]
    preparation = await executor.execute_many(preparation_commands)
    preparation_ok = len(preparation) == len(preparation_commands) and all(
        result.ok for result in preparation
    )
    aborted = False
    follow: list[Any] = []
    if preparation_ok and chunk_sync_delay_sec > 0:
        aborted = await _wait_for_stop(stop_event, chunk_sync_delay_sec)
    if preparation_ok and not aborted:
        follow = await executor.execute_many(
            [f"/spectate {target_username} {spectator_player}"]
        )
    follow_ok = len(follow) == 1 and all(result.ok for result in follow)
    preparation_payload = [result.to_json() for result in preparation]
    follow_payload = [result.to_json() for result in follow]
    return {
        "success": preparation_ok and follow_ok and not aborted,
        "aborted": aborted,
        "preparation_commands": preparation_payload,
        "follow_commands": follow_payload,
        "commands": [*preparation_payload, *follow_payload],
        "chunk_sync_delay_sec": chunk_sync_delay_sec,
        "reset_spectator_mode": reset_spectator_mode,
    }


def _camera_state_change_trigger(
    previous: LiveAgentCameraState | None,
    current: LiveAgentCameraState | None,
) -> str | None:
    """Name a camera-invalidating target change while tolerating partial observations."""

    if previous is None or current is None:
        return None
    checks = (
        ("spawn_sequence", "target_spawn_sequence_changed"),
        ("entity_id", "target_entity_changed"),
        ("dimension", "target_dimension_changed"),
    )
    for field_name, trigger in checks:
        before = getattr(previous, field_name)
        after = getattr(current, field_name)
        if before is not None and after is not None and before != after:
            return trigger
    return None


def _merge_camera_state(
    previous: LiveAgentCameraState | None,
    current: LiveAgentCameraState,
) -> LiveAgentCameraState:
    """Preserve known identity fields when a later compact observation omits them."""

    if previous is None or previous.run_id != current.run_id:
        return current
    return LiveAgentCameraState(
        run_id=current.run_id,
        entity_id=current.entity_id
        if current.entity_id is not None
        else previous.entity_id,
        spawn_sequence=(
            current.spawn_sequence
            if current.spawn_sequence is not None
            else previous.spawn_sequence
        ),
        position=current.position
        if current.position is not None
        else previous.position,
        dimension=current.dimension
        if current.dimension is not None
        else previous.dimension,
    )


async def _probe_spectator_distance(
    *,
    executor: RconServerCommandExecutor,
    spectator_player: str,
    target_username: str,
) -> dict[str, Any]:
    """Read both server positions without changing camera state."""

    results = await executor.execute_many(
        [
            f"/data get entity {spectator_player} Pos",
            f"/data get entity {target_username} Pos",
        ]
    )
    spectator_position = (
        _parse_entity_position_response(getattr(results[0], "response", None))
        if len(results) >= 1 and results[0].ok
        else None
    )
    target_position = (
        _parse_entity_position_response(getattr(results[1], "response", None))
        if len(results) >= 2 and results[1].ok
        else None
    )
    horizontal_distance: float | None = None
    if spectator_position is not None and target_position is not None:
        horizontal_distance = math.hypot(
            spectator_position[0] - target_position[0],
            spectator_position[2] - target_position[2],
        )
    return {
        "success": horizontal_distance is not None,
        "spectator_position": spectator_position,
        "target_position": target_position,
        "horizontal_distance_blocks": horizontal_distance,
        "commands": [result.to_json() for result in results],
    }


async def _wait_for_run_event_or_stop(
    *,
    run_event_stream: LiveRunEventStream,
    stop_event: asyncio.Event,
    timeout_sec: float | None,
) -> tuple[LiveRunStartedEvent | None, bool]:
    """Wait for a post-reset run signal, shutdown, or a keepalive timeout."""

    event_task = asyncio.create_task(run_event_stream.next())
    stop_task = asyncio.create_task(stop_event.wait())
    tasks = {event_task, stop_task}
    try:
        done, _ = await asyncio.wait(
            tasks,
            timeout=timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and stop_task.result():
            return None, True
        if event_task in done:
            return event_task.result(), False
        return None, False
    finally:
        pending = [task for task in tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _audit_spectator_attempt(
    payload: dict[str, Any],
    callback: SpectatorAuditCallback | None,
) -> None:
    """Persist one camera synchronization attempt without breaking live execution on audit errors."""

    if callback is None:
        return
    try:
        await callback(str(payload["run_id"]), "spectator_follow_attempt", payload)
    except Exception as exc:  # noqa: BLE001 - camera follow must survive a secondary audit failure.
        payload["audit_error"] = f"{type(exc).__name__}: {exc}"


async def _wait_for_stop(stop_event: asyncio.Event, delay_sec: float) -> bool:
    """Wait for spectator shutdown and report whether it arrived before the delay."""

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_sec)
    except TimeoutError:
        return False
    return True


async def _restore_spectator_player(
    *,
    executor: RconServerCommandExecutor,
    spectator_player: str,
) -> list[dict[str, Any]]:
    """Return the recording camera client to a normal visible player mode."""

    results = await executor.execute_many([f"/gamemode creative {spectator_player}"])
    return [{"attempt": 1, "commands": [result.to_json() for result in results]}]


async def _finish_background_spectate(
    task: asyncio.Task[list[dict[str, Any]]],
    *,
    timeout_sec: float,
    cancel_on_timeout: bool,
) -> list[dict[str, Any]]:
    """Collect a spectate/restore task without leaking cancellation errors."""

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_sec)
    except TimeoutError:
        if cancel_on_timeout:
            task.cancel()
        return [{"timeout": True, "cancelled": cancel_on_timeout}]
    except asyncio.CancelledError:
        return [{"cancelled": True}]
    except Exception as exc:  # noqa: BLE001 - background RCON failures belong in report metadata.
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _worker_recovery_callback(
    supervisors: dict[str, WorkerProcessSupervisor],
) -> Callable[[LiveWorkerSpec, LiveTrainingOutcome], Awaitable[dict[str, Any]]]:
    """Build the runner callback that restarts one local worker before task requeue."""

    async def recover(
        worker: LiveWorkerSpec,
        outcome: LiveTrainingOutcome,
    ) -> dict[str, Any]:
        """Restart the worker assigned to the failed attempt on its stable endpoint."""

        supervisor = supervisors.get(worker.worker_id)
        if supervisor is None:
            return {
                "success": False,
                "reason": "worker_supervisor_not_found",
                "worker_id": worker.worker_id,
            }
        result = await asyncio.to_thread(supervisor.restart)
        return {
            **result,
            "failure_class": outcome.failure_class,
            "source_run_id": outcome.run_id,
        }

    return recover


def _start_worker(port: int, *, stdout: TextIO | None = None) -> subprocess.Popen[str]:
    """Start one Mineflayer worker process on a local WebSocket port."""

    env = os.environ.copy()
    env["MINEFLAYER_WORKER_PORT"] = str(port)
    return subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=ROOT / "workers" / "mineflayer-worker",
        env=env,
        stdout=stdout or subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _wait_for_tcp(host: str, port: int, timeout_sec: float) -> None:
    """Wait until a TCP port accepts connections."""

    def connect_once() -> None:
        with socket.create_connection((host, port), timeout=1):
            return None

    _wait_for(connect_once, timeout_sec=timeout_sec)


def _wait_for(operation: Callable[[], Any], timeout_sec: float) -> Any:
    """Retry an operation until success or timeout."""

    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return operation()
        except (ConnectionError, OSError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for service: {last_error}")


def _free_port() -> int:
    """Reserve and return a currently free local TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate one child process group so npm does not leave its Node child alive."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def as_json(worker: LiveWorkerSpec) -> dict[str, Any]:
    """Convert one worker spec into JSON-safe output."""

    return {
        "worker_id": worker.worker_id,
        "worker_url": worker.worker_url,
        "username": worker.username,
        "server_id": worker.server_id,
        "minecraft_host": worker.minecraft_host,
        "minecraft_port": worker.minecraft_port,
        "rcon_host": worker.rcon_host,
        "rcon_port": worker.rcon_port,
        "world_dir": worker.world_dir,
    }


def _server_pool_payload(
    server_pool: list[MinecraftServerInstanceSpec],
    workers: list[LiveWorkerSpec],
    *,
    state_path: Path | None,
    state_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build auditable isolated server/worker placement and resource estimates."""

    estimate = estimate_server_pool_resources(server_pool)
    isolated = len(server_pool) == len(workers) and all(
        server.max_workers == 1 for server in server_pool
    )
    return {
        "mode": "isolated_server_per_worker"
        if isolated
        else "single_server_multi_worker",
        "state_path": str(state_path) if state_path is not None else None,
        "state_started_at": state_payload.get("started_at")
        if isinstance(state_payload, dict)
        else None,
        "instances": [server.to_json() for server in server_pool],
        "placements": [
            {
                "worker_id": worker.worker_id,
                "server_id": worker.server_id,
                "game_endpoint": f"{worker.minecraft_host}:{worker.minecraft_port}",
                "rcon_endpoint": f"{worker.rcon_host}:{worker.rcon_port}"
                if worker.rcon_port is not None
                else None,
                "world_dir": worker.world_dir,
            }
            for worker in workers
        ],
        "resource_estimate": estimate.to_json(),
        "isolation_enforced": isolated,
    }


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build compact terminal output from the full live training payload."""

    return {
        "job_id": payload["job_id"],
        "status": payload["status"],
        "task_count": payload["task_count"],
        "attempt_count": payload["attempt_count"],
        "retried_task_count": payload["retried_task_count"],
        "success_count": payload["success_count"],
        "auto_promote": payload["auto_promote"],
        "model_usage": payload["model_usage"],
        "task_ids": payload["task_ids"],
        "workers": payload["workers"],
        "database_url": payload["database_url"],
        "output_path": payload["output_path"],
        "recording": {
            "enabled": payload.get("recording", {}).get("enabled"),
            "output_path": payload.get("recording", {}).get("output_path"),
            "ffmpeg_started": payload.get("recording", {}).get("ffmpeg_started"),
            "error": payload.get("recording", {}).get("error"),
        },
    }


if __name__ == "__main__":
    main()
