from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from mc_agent_harness.evaluation.mineclip import MineClipScorer, MineClipScorerError
from mc_agent_harness.harness.evaluation import EvaluationRecorder


@dataclass(frozen=True, slots=True)
class FrameArtifact:
    """One immutable first-person frame and its capture ordering metadata."""

    path: Path
    sequence: int
    captured_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert frame metadata into an audit-safe payload without embedding image bytes."""

        return {
            "path": str(self.path),
            "sequence": self.sequence,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True, slots=True)
class FrameSamplingPolicy:
    """Window construction policy for MineCLIP's fixed 16-frame input."""

    clip_length: int = 16
    window_stride: int = 8
    max_windows: int = 64
    key_frame_count: int = 3
    sample_fps: float = 2.0

    @classmethod
    def from_verifier(cls, verifier: dict[str, Any]) -> FrameSamplingPolicy:
        """Load a bounded sampling policy from creative verifier metadata."""

        raw = verifier.get("frame_sampling")
        payload = raw if isinstance(raw, dict) else {}
        return cls(
            clip_length=int(payload.get("clip_length", 16)),
            window_stride=max(1, int(payload.get("window_stride", 8))),
            max_windows=max(1, int(payload.get("max_windows", 64))),
            key_frame_count=max(1, int(payload.get("key_frame_count", 3))),
            sample_fps=max(0.1, float(payload.get("sample_fps", 2.0))),
        )


@dataclass(frozen=True, slots=True)
class CreativeWindowScore:
    """MineCLIP evidence for one temporal frame window."""

    window_index: int
    start_sequence: int
    end_sequence: int
    center_frame: dict[str, Any]
    target_probability: float
    scorer: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        """Convert one score-trend point into a JSON-safe dictionary."""

        return asdict(self)


class CreativeTaskEvaluator:
    """External creative-task evaluator built around MineCLIP trajectory scoring."""

    def __init__(
        self,
        scorer: MineClipScorer,
        *,
        recorder: EvaluationRecorder | None = None,
        calibration_registry: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Configure scoring, optional audit persistence, and threshold overlays."""

        self.scorer = scorer
        self.recorder = recorder
        self.calibration_registry = calibration_registry or {}

    async def evaluate(
        self,
        task_spec: dict[str, Any],
        frames: Iterable[FrameArtifact | str | Path | dict[str, Any]],
        *,
        run_id: str,
        evidence_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Score one completed trajectory and retain guarded media evidence for review."""

        verifier = _creative_verifier(task_spec)
        prompt = str(verifier["prompt"])
        negative_prompts = [str(value) for value in verifier.get("negative_prompts", [])]
        policy = FrameSamplingPolicy.from_verifier(verifier)
        artifacts = _normalize_frames(frames)
        windows = _sample_windows(artifacts, policy)
        threshold, calibration = _resolve_calibration(
            str(task_spec.get("task_id") or "unknown"),
            verifier,
            self.calibration_registry,
        )
        await self._record(
            run_id,
            "creative_evaluation_started",
            {
                "task_id": task_spec.get("task_id"),
                "prompt": prompt,
                "negative_prompts": negative_prompts,
                "frame_count": len(artifacts),
                "window_count": len(windows),
                "frame_sampling": asdict(policy),
                "calibration": calibration,
                "evaluator_visibility": "external_not_agent_context",
            },
        )
        if policy.clip_length != 16:
            return await self._inconclusive(
                run_id,
                task_spec,
                prompt,
                artifacts,
                calibration,
                "MineCLIP requires clip_length=16.",
                evidence_source=evidence_source,
            )
        if not windows:
            return await self._inconclusive(
                run_id,
                task_spec,
                prompt,
                artifacts,
                calibration,
                f"Need at least {policy.clip_length} captured frames for MineCLIP.",
                evidence_source=evidence_source,
            )

        trend: list[CreativeWindowScore] = []
        try:
            for index, window in enumerate(windows):
                score = await self.scorer.score(
                    [frame.path.read_bytes() for frame in window],
                    prompt,
                    negative_prompts,
                )
                center = window[len(window) // 2]
                point = CreativeWindowScore(
                    window_index=index,
                    start_sequence=window[0].sequence,
                    end_sequence=window[-1].sequence,
                    center_frame=center.to_json(),
                    target_probability=score.target_probability,
                    scorer={
                        "name": score.scorer,
                        "variant": score.variant,
                        "checkpoint_checksum": score.checkpoint_checksum,
                        "latency_ms": score.latency_ms,
                        "logits": list(score.logits),
                        "probabilities": list(score.probabilities),
                        "metadata": score.metadata,
                    },
                )
                trend.append(point)
                await self._record(
                    run_id,
                    "creative_frame_window_scored",
                    {"task_id": task_spec.get("task_id"), **point.to_json()},
                )
        except (MineClipScorerError, OSError, ValueError) as exc:
            return await self._inconclusive(
                run_id,
                task_spec,
                prompt,
                artifacts,
                calibration,
                f"Creative scoring failed: {type(exc).__name__}: {exc}",
                trend=trend,
                evidence_source=evidence_source,
            )

        trajectory_score = statistics.fmean(point.target_probability for point in trend)
        success = trajectory_score > threshold if threshold is not None else False
        inconclusive = threshold is None
        key_frames = _key_frames(trend, policy.key_frame_count)
        result = {
            "success": success,
            "inconclusive": inconclusive,
            "reason": (
                f"Trajectory mean MineCLIP probability {trajectory_score:.6f} "
                f"{'exceeded' if success else 'did not exceed'} calibrated threshold {threshold:.6f}."
                if threshold is not None
                else "MineCLIP score was computed, but this task has no calibrated threshold."
            ),
            "type": "creative_mineclip",
            "task_id": task_spec.get("task_id"),
            "prompt": prompt,
            "score": trajectory_score,
            "score_threshold": threshold,
            "aggregation": "trajectory_mean",
            "frame_count": len(artifacts),
            "window_count": len(trend),
            "score_trend": [point.to_json() for point in trend],
            "key_frames": key_frames,
            "final_frame": _final_frame(artifacts, trend),
            "evidence_source": evidence_source,
            "calibration": calibration,
            "checks": [
                {
                    "type": "creative_mineclip",
                    "success": success,
                    "inconclusive": inconclusive,
                    "score": trajectory_score,
                    "score_threshold": threshold,
                }
            ],
        }
        await self._record(run_id, "creative_evaluation_completed", result)
        return result

    async def _inconclusive(
        self,
        run_id: str,
        task_spec: dict[str, Any],
        prompt: str,
        frames: list[FrameArtifact],
        calibration: dict[str, Any],
        reason: str,
        *,
        trend: list[CreativeWindowScore] | None = None,
        evidence_source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one non-authoritative result when evidence is unavailable."""

        result = creative_inconclusive_result(
            task_spec,
            reason=reason,
            calibration=calibration,
            frame_count=len(frames),
            trend=trend,
            prompt=prompt,
            evidence_source=evidence_source,
        )
        if frames:
            result["final_frame"] = frames[-1].to_json()
        await self._record(run_id, "creative_evaluation_inconclusive", result)
        return result

    async def _record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Write an evaluation event when an audit recorder is configured."""

        if self.recorder is not None:
            await self.recorder.record(run_id, event_type, payload)


def creative_inconclusive_result(
    task_spec: dict[str, Any],
    *,
    reason: str,
    calibration: dict[str, Any] | None = None,
    frame_count: int = 0,
    trend: list[CreativeWindowScore] | None = None,
    prompt: str | None = None,
    source_validation: dict[str, Any] | None = None,
    evidence_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard non-authoritative result without invoking MineCLIP."""

    verifier = _creative_verifier(task_spec)
    resolved_calibration = dict(calibration or {})
    resolved_trend = list(trend or [])
    checks: list[dict[str, Any]] = []
    if source_validation is not None:
        checks.append(
            {
                "type": "creative_source_validation",
                "success": False,
                "inconclusive": True,
                "validation": source_validation,
            }
        )
    return {
        "success": False,
        "inconclusive": True,
        "reason": reason,
        "type": "creative_mineclip",
        "task_id": task_spec.get("task_id"),
        "prompt": prompt or str(verifier["prompt"]),
        "score": None,
        "score_threshold": resolved_calibration.get("score_threshold"),
        "aggregation": "trajectory_mean",
        "frame_count": frame_count,
        "window_count": len(resolved_trend),
        "score_trend": [point.to_json() for point in resolved_trend],
        "key_frames": _key_frames(resolved_trend, 3),
        "final_frame": _final_frame(frames=[], trend=resolved_trend),
        "evidence_source": evidence_source,
        "calibration": resolved_calibration,
        "source_validation": source_validation,
        "checks": checks,
    }


def _creative_verifier(task_spec: dict[str, Any]) -> dict[str, Any]:
    """Return and validate the creative verifier section of a task manifest."""

    verifier = task_spec.get("verifier") or task_spec.get("success_criteria")
    if not isinstance(verifier, dict) or verifier.get("type") != "creative_mineclip":
        raise ValueError("Task does not define a creative_mineclip verifier.")
    if not isinstance(verifier.get("prompt"), str) or not verifier["prompt"].strip():
        raise ValueError("creative_mineclip verifier requires a prompt.")
    if not isinstance(verifier.get("negative_prompts"), list) or not verifier["negative_prompts"]:
        raise ValueError("creative_mineclip verifier requires negative_prompts.")
    return verifier


def _normalize_frames(
    frames: Iterable[FrameArtifact | str | Path | dict[str, Any]],
) -> list[FrameArtifact]:
    """Normalize frame paths and metadata into a stable sequence."""

    normalized: list[FrameArtifact] = []
    for index, value in enumerate(frames):
        if isinstance(value, FrameArtifact):
            artifact = value
        elif isinstance(value, dict):
            artifact = FrameArtifact(
                path=Path(str(value.get("path") or "")),
                sequence=int(value.get("sequence", index)),
                captured_at=str(value["captured_at"]) if value.get("captured_at") else None,
            )
        else:
            artifact = FrameArtifact(path=Path(value), sequence=index)
        if artifact.path.is_file():
            normalized.append(artifact)
    return sorted(normalized, key=lambda frame: (frame.sequence, str(frame.path)))


def _sample_windows(
    frames: list[FrameArtifact],
    policy: FrameSamplingPolicy,
) -> list[list[FrameArtifact]]:
    """Build uniformly bounded sliding windows while preserving trajectory coverage."""

    if len(frames) < policy.clip_length:
        return []
    starts = list(range(0, len(frames) - policy.clip_length + 1, policy.window_stride))
    final_start = len(frames) - policy.clip_length
    if starts[-1] != final_start:
        starts.append(final_start)
    if len(starts) > policy.max_windows:
        starts = _uniform_select(starts, policy.max_windows)
    return [frames[start : start + policy.clip_length] for start in starts]


def _uniform_select(values: list[int], count: int) -> list[int]:
    """Select ordered indices that cover a long trajectory from start through finish."""

    if count >= len(values):
        return values
    if count == 1:
        return [values[-1]]
    indexes = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in indexes]


def _resolve_calibration(
    task_id: str,
    verifier: dict[str, Any],
    registry: dict[str, dict[str, Any]],
) -> tuple[float | None, dict[str, Any]]:
    """Resolve a reviewed calibration overlay before falling back to manifest metadata."""

    manifest = verifier.get("calibration") if isinstance(verifier.get("calibration"), dict) else {}
    overlay = registry.get(task_id) if isinstance(registry.get(task_id), dict) else {}
    calibration = {**manifest, **overlay}
    raw_threshold = overlay.get("score_threshold", verifier.get("score_threshold"))
    if raw_threshold is None:
        raw_threshold = calibration.get("score_threshold")
    threshold = float(raw_threshold) if isinstance(raw_threshold, (int, float)) else None
    calibration["score_threshold"] = threshold
    calibration["status"] = "calibrated" if threshold is not None else str(
        calibration.get("status") or "pending"
    )
    return threshold, calibration


def _key_frames(trend: list[CreativeWindowScore], count: int) -> list[dict[str, Any]]:
    """Return unique center frames from the highest-scoring windows."""

    ranked = sorted(trend, key=lambda point: point.target_probability, reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for point in ranked:
        path = str(point.center_frame.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        selected.append(
            {
                **point.center_frame,
                "score": point.target_probability,
                "window_index": point.window_index,
            }
        )
        if len(selected) >= count:
            break
    return selected


def _final_frame(
    frames: list[FrameArtifact],
    trend: list[CreativeWindowScore],
) -> dict[str, Any] | None:
    """Return the terminal visual frame separately from highest-scoring evidence frames."""

    if frames:
        frame = frames[-1].to_json()
        frame["score"] = trend[-1].target_probability if trend else None
        return frame
    if trend:
        frame = dict(trend[-1].center_frame)
        frame["score"] = trend[-1].target_probability
        return frame
    return None
