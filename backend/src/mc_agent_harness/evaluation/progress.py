from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mc_agent_harness.evaluation.mineclip import MineClipScorer, MineClipScorerError
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.runtime.visual_snapshot import VisualFrameProvider
from mc_agent_harness.schemas.action import HarnessAction


@dataclass(frozen=True, slots=True)
class CreativeProgressPolicy:
    """Bounded sampling and queue policy for non-authoritative MineCLIP feedback."""

    important_actions: tuple[str, ...] = ("place_block", "dig_block_at", "use_item")
    sample_fps: float = 2.0
    clip_length: int = 16
    ring_buffer_frames: int = 64
    post_action_frames: int = 2
    max_pending_jobs: int = 2
    min_checkpoint_interval_sec: float = 3.0
    window_wait_timeout_sec: float = 20.0
    meaningful_delta: float = 0.02

    def __post_init__(self) -> None:
        """Reject policies that could create unbounded capture or scorer work."""

        if self.sample_fps <= 0:
            raise ValueError("sample_fps must be positive.")
        if self.clip_length != 16:
            raise ValueError("MineCLIP progress feedback requires clip_length=16.")
        if self.ring_buffer_frames < self.clip_length:
            raise ValueError("ring_buffer_frames must be at least clip_length.")
        if self.post_action_frames < 0:
            raise ValueError("post_action_frames cannot be negative.")
        if self.max_pending_jobs < 1:
            raise ValueError("max_pending_jobs must be positive.")
        if self.window_wait_timeout_sec <= 0:
            raise ValueError("window_wait_timeout_sec must be positive.")


@dataclass(frozen=True, slots=True)
class ProgressFrame:
    """One trusted Minecraft window frame retained in the online ring buffer."""

    sequence: int
    data: bytes
    size_bytes: int
    captured_at: str


@dataclass(frozen=True, slots=True)
class ProgressCheckpoint:
    """An important action marker waiting for enough post-action visual evidence."""

    job_id: str
    action_type: str
    action_sequence: int
    requested_after_frame: int
    requested_at: str
    baseline: bool = False


class CreativeProgressMonitor:
    """Continuously capture frames and asynchronously score important action checkpoints."""

    def __init__(
        self,
        frame_provider: VisualFrameProvider,
        scorer: MineClipScorer,
        *,
        policy: CreativeProgressPolicy | None = None,
        readiness_event: asyncio.Event | None = None,
    ) -> None:
        """Configure one task-local ring buffer without starting background work yet."""

        self.frame_provider = frame_provider
        self.scorer = scorer
        self.policy = policy or CreativeProgressPolicy()
        self.readiness_event = readiness_event
        self._frames: deque[ProgressFrame] = deque(maxlen=self.policy.ring_buffer_frames)
        self._condition = asyncio.Condition()
        self._queue: asyncio.Queue[ProgressCheckpoint] = asyncio.Queue(
            maxsize=self.policy.max_pending_jobs + 1
        )
        self._sampler_task: asyncio.Task[None] | None = None
        self._scorer_task: asyncio.Task[None] | None = None
        self._active = False
        self._prompt = ""
        self._negative_prompts: list[str] = []
        self._frame_sequence = -1
        self._action_sequence = 0
        self._active_job_id: str | None = None
        self._latest_feedback: dict[str, Any] | None = None
        self._baseline_score: float | None = None
        self._previous_score: float | None = None
        self._last_checkpoint_at = 0.0
        self._last_capture_error: str | None = None

    @property
    def active(self) -> bool:
        """Return whether the current task has a valid creative MineCLIP verifier."""

        return self._active

    async def start(self, task_spec: dict[str, Any]) -> None:
        """Start a continuous sampler for one creative task and enqueue a baseline score."""

        await self.stop()
        verifier = _creative_verifier(task_spec)
        if verifier is None:
            return
        self._prompt = str(verifier["prompt"])
        self._negative_prompts = [str(value) for value in verifier["negative_prompts"]]
        self._frames = deque(maxlen=self.policy.ring_buffer_frames)
        self._condition = asyncio.Condition()
        self._queue = asyncio.Queue(maxsize=self.policy.max_pending_jobs + 1)
        self._frame_sequence = -1
        self._action_sequence = 0
        self._active_job_id = None
        self._latest_feedback = None
        self._baseline_score = None
        self._previous_score = None
        self._last_checkpoint_at = 0.0
        self._last_capture_error = None
        self._active = True
        self._sampler_task = asyncio.create_task(
            self._sample_loop(),
            name="creative-progress-frame-sampler",
        )
        self._scorer_task = asyncio.create_task(
            self._score_loop(),
            name="creative-progress-mineclip-scorer",
        )
        await self._queue.put(self._new_checkpoint("baseline", baseline=True))

    async def checkpoint(
        self,
        action: HarnessAction,
        action_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Queue an important successful action without delaying its runtime response."""

        if not self._active or action.type not in self.policy.important_actions:
            return None
        if action_result.get("ok") is not True:
            return None
        now = time.monotonic()
        if now - self._last_checkpoint_at < self.policy.min_checkpoint_interval_sec:
            return {
                "status": "coalesced",
                "reason": "minimum_checkpoint_interval",
                "action_type": action.type,
                "advisory_only": True,
            }
        if self._queue.qsize() >= self.policy.max_pending_jobs:
            return {
                "status": "coalesced",
                "reason": "progress_queue_full",
                "action_type": action.type,
                "advisory_only": True,
            }
        self._last_checkpoint_at = now
        checkpoint = self._new_checkpoint(action.type)
        await self._queue.put(checkpoint)
        return {
            "job_id": checkpoint.job_id,
            "status": "queued",
            "action_type": action.type,
            "requested_after_frame": checkpoint.requested_after_frame,
            "blocking": False,
            "advisory_only": True,
        }

    async def snapshot(self) -> dict[str, Any] | None:
        """Return the latest completed advisory result and current queue pressure."""

        if not self._active:
            return None
        payload = {
            "latest": dict(self._latest_feedback) if self._latest_feedback is not None else None,
            "pending_jobs": self._queue.qsize() + (1 if self._active_job_id else 0),
            "captured_frames": len(self._frames),
            "buffer_ready": len(self._frames) >= self.policy.clip_length,
            "last_capture_error": self._last_capture_error,
            "advisory_only": True,
            "success_authority": "human_review",
        }
        return payload

    async def stop(self) -> None:
        """Cancel sampler/scorer tasks without waiting for queued evaluations to block shutdown."""

        self._active = False
        tasks = [task for task in (self._sampler_task, self._scorer_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sampler_task = None
        self._scorer_task = None
        self._active_job_id = None

    def _new_checkpoint(self, action_type: str, *, baseline: bool = False) -> ProgressCheckpoint:
        """Build one checkpoint against the latest observed ring-buffer sequence."""

        checkpoint = ProgressCheckpoint(
            job_id=f"mineclip-progress-{uuid.uuid4().hex[:16]}",
            action_type=action_type,
            action_sequence=self._action_sequence,
            requested_after_frame=self._frame_sequence,
            requested_at=datetime.now(tz=UTC).isoformat(),
            baseline=baseline,
        )
        self._action_sequence += 1
        return checkpoint

    async def _sample_loop(self) -> None:
        """Capture trusted frames at a fixed low rate into a bounded ring buffer."""

        if self.readiness_event is not None:
            await self.readiness_event.wait()
        interval = 1.0 / self.policy.sample_fps
        while self._active:
            started = time.monotonic()
            try:
                capture = await self.frame_provider.capture()
                raw_path = capture.get("artifact_path") or capture.get("image")
                path = Path(str(raw_path)).expanduser().resolve()
                if not path.is_file():
                    raise FileNotFoundError(f"Captured progress frame is unavailable: {path}")
                self._frame_sequence += 1
                data = await asyncio.to_thread(path.read_bytes)
                path.unlink(missing_ok=True)
                frame = ProgressFrame(
                    sequence=self._frame_sequence,
                    data=data,
                    size_bytes=len(data),
                    captured_at=datetime.now(tz=UTC).isoformat(),
                )
                async with self._condition:
                    self._frames.append(frame)
                    self._last_capture_error = None
                    self._condition.notify_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - capture errors remain advisory and audited.
                self._last_capture_error = f"{type(exc).__name__}: {exc}"
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.01, interval - elapsed))

    async def _score_loop(self) -> None:
        """Score queued checkpoints serially so MineCLIP cannot overload the local machine."""

        while self._active:
            checkpoint = await self._queue.get()
            self._active_job_id = checkpoint.job_id
            try:
                frames = await self._wait_for_window(checkpoint)
                score = await self.scorer.score(
                    [frame.data for frame in frames],
                    self._prompt,
                    self._negative_prompts,
                )
                previous = self._previous_score
                delta = score.target_probability - previous if previous is not None else None
                if checkpoint.baseline:
                    self._baseline_score = score.target_probability
                self._previous_score = score.target_probability
                self._latest_feedback = {
                    "job_id": checkpoint.job_id,
                    "status": "completed",
                    "action_type": checkpoint.action_type,
                    "action_sequence": checkpoint.action_sequence,
                    "score": score.target_probability,
                    "score_delta": delta,
                    "baseline_score": self._baseline_score,
                    "trend": _score_trend(delta, self.policy.meaningful_delta),
                    "confidence": "low",
                    "advisory_only": True,
                    "success_authority": "human_review",
                    "frame_window": {
                        "start_sequence": frames[0].sequence,
                        "end_sequence": frames[-1].sequence,
                        "frame_count": len(frames),
                    },
                    "scorer": {
                        "name": score.scorer,
                        "variant": score.variant,
                        "latency_ms": score.latency_ms,
                        "checkpoint_checksum": score.checkpoint_checksum,
                    },
                    "summary": _progress_summary(
                        checkpoint.action_type,
                        score.target_probability,
                        delta,
                        self.policy.meaningful_delta,
                    ),
                }
            except asyncio.CancelledError:
                raise
            except (MineClipScorerError, OSError, ValueError, TimeoutError) as exc:
                self._latest_feedback = {
                    "job_id": checkpoint.job_id,
                    "status": "failed",
                    "action_type": checkpoint.action_type,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "advisory_only": True,
                    "success_authority": "human_review",
                }
            finally:
                self._active_job_id = None
                self._queue.task_done()

    async def _wait_for_window(self, checkpoint: ProgressCheckpoint) -> list[ProgressFrame]:
        """Wait for a full clip that includes bounded post-action visual evidence."""

        minimum_end = checkpoint.requested_after_frame + self.policy.post_action_frames

        def ready() -> bool:
            """Return whether the ring buffer can satisfy this checkpoint."""

            return (
                len(self._frames) >= self.policy.clip_length
                and self._frames[-1].sequence >= minimum_end
            )

        async with self._condition:
            await asyncio.wait_for(
                self._condition.wait_for(ready),
                timeout=self.policy.window_wait_timeout_sec,
            )
            return list(self._frames)[-self.policy.clip_length :]


class CreativeProgressFeedbackRuntime:
    """Runtime decorator that returns queued jobs immediately and feedback on later observations."""

    def __init__(self, runtime: GameRuntime, monitor: CreativeProgressMonitor) -> None:
        """Bind online MineCLIP feedback to a runtime without changing worker JSON-RPC."""

        self.runtime = runtime
        self.monitor = monitor

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any] | None:
        """Reset Minecraft first, then start a task-scoped visual ring buffer."""

        result = await self.runtime.reset(task_spec)
        await self.monitor.start(task_spec)
        return result

    async def observe(self) -> dict[str, Any]:
        """Attach the latest completed advisory score to normal structured observation."""

        observation = await self.runtime.observe()
        progress = await self.monitor.snapshot()
        if progress is not None:
            observation["creative_progress"] = progress
        return observation

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Execute an action immediately, then enqueue eligible visual scoring work."""

        result = await self.runtime.act(action)
        job = await self.monitor.checkpoint(action, result)
        if job is not None:
            result["creative_progress_job"] = job
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Attach process-feedback metadata to explicit runtime snapshots."""

        snapshot = await self.runtime.snapshot()
        progress = await self.monitor.snapshot()
        if progress is not None:
            snapshot["creative_progress"] = progress
        return snapshot

    async def close(self) -> None:
        """Stop advisory background work before closing the worker runtime."""

        await self.monitor.stop()
        await self.runtime.close()


def _creative_verifier(task_spec: dict[str, Any]) -> dict[str, Any] | None:
    """Return a usable MineCLIP verifier only for creative tasks."""

    if task_spec.get("category") != "creative":
        return None
    verifier = task_spec.get("verifier") or task_spec.get("success_criteria")
    if not isinstance(verifier, dict) or verifier.get("type") != "creative_mineclip":
        return None
    prompt = verifier.get("prompt")
    negatives = verifier.get("negative_prompts")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if not isinstance(negatives, list) or not negatives:
        return None
    return verifier


def _score_trend(delta: float | None, meaningful_delta: float) -> str:
    """Convert a noisy score change into a conservative qualitative trend."""

    if delta is None:
        return "baseline"
    if delta >= meaningful_delta:
        return "improving"
    if delta <= -meaningful_delta:
        return "regressing"
    return "stable"


def _progress_summary(
    action_type: str,
    score: float,
    delta: float | None,
    meaningful_delta: float,
) -> str:
    """Build model-facing feedback that explicitly avoids claiming task correctness."""

    trend = _score_trend(delta, meaningful_delta)
    delta_text = "baseline" if delta is None else f"delta {delta:+.4f}"
    return (
        f"MineCLIP advisory after {action_type}: alignment {score:.4f}, {delta_text}, "
        f"trend {trend}. This is noisy semantic-alignment evidence, not proof of correctness "
        "or task completion."
    )
