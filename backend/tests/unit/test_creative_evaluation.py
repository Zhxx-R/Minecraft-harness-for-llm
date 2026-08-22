from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from mc_agent_harness.evaluation.calibration import calibrate_creative_threshold
from mc_agent_harness.evaluation.creative import CreativeTaskEvaluator, FrameArtifact
from mc_agent_harness.evaluation.mineclip import MineClipScore, MineClipScorer
from mc_agent_harness.evaluation.verifiers import ProgrammaticVerifier
from mc_agent_harness.harness.evaluation import EvaluationRecorder


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


class SequenceScorer:
    """Deterministic MineCLIP scorer test double with one score per sampled window."""

    def __init__(self, scores: list[float]) -> None:
        """Store the score sequence and capture submitted requests."""

        self.scores = iter(scores)
        self.calls: list[dict[str, Any]] = []

    async def score(
        self,
        frames: list[bytes],
        prompt: str,
        negative_prompts: list[str],
    ) -> MineClipScore:
        """Return the next configured score while retaining request evidence."""

        self.calls.append(
            {
                "frame_count": len(frames),
                "prompt": prompt,
                "negative_prompts": list(negative_prompts),
            }
        )
        score = next(self.scores)
        return MineClipScore(
            target_probability=score,
            prompt=prompt,
            negative_prompts=tuple(negative_prompts),
            logits=(score, 1 - score),
            probabilities=(score, 1 - score),
            scorer="mineclip_official",
            variant="attn",
            checkpoint_checksum="verified-checksum",
            latency_ms=5.0,
        )


def _task_spec(*, threshold: float | None) -> dict[str, Any]:
    """Build one compact creative manifest for evaluator tests."""

    return {
        "task_id": "creative:test",
        "category": "creative",
        "verifier": {
            "type": "creative_mineclip",
            "prompt": "Build a stone tower",
            "negative_prompts": ["Create a flower garden"],
            "score_threshold": threshold,
            "frame_sampling": {
                "clip_length": 16,
                "window_stride": 8,
                "max_windows": 8,
                "key_frame_count": 2,
                "sample_fps": 2.0,
            },
            "calibration": {"status": "calibrated" if threshold is not None else "pending"},
        },
    }


def _frames(directory: Path, count: int) -> list[FrameArtifact]:
    """Create small frame files sufficient for a scorer test double."""

    artifacts: list[FrameArtifact] = []
    for index in range(count):
        path = directory / f"frame_{index:03d}.jpg"
        path.write_bytes(f"frame-{index}".encode("ascii"))
        artifacts.append(FrameArtifact(path=path, sequence=index))
    return artifacts


def test_calibration_uses_two_centroid_midpoint() -> None:
    """Reviewed low/high score clusters produce a deterministic threshold and metrics."""

    result = calibrate_creative_threshold(
        [0.1, 0.2, 0.8, 0.9],
        human_labels=[False, False, True, True],
    )

    assert result.lower_centroid == pytest.approx(0.15)
    assert result.upper_centroid == pytest.approx(0.85)
    assert result.threshold == pytest.approx(0.5)
    assert result.f1 == pytest.approx(1.0)
    assert result.accuracy == pytest.approx(1.0)


@pytest.mark.anyio
async def test_creative_evaluator_scores_windows_and_records_audit(tmp_path: Path) -> None:
    """Trajectory scoring averages 16-frame windows and preserves trend/key-frame evidence."""

    scorer = SequenceScorer([0.4, 0.8])
    recorder = EvaluationRecorder()
    evaluator = CreativeTaskEvaluator(scorer, recorder=recorder)  # type: ignore[arg-type]

    result = await evaluator.evaluate(_task_spec(threshold=0.5), _frames(tmp_path, 24), run_id="run-1")

    assert result["success"] is True
    assert result["inconclusive"] is False
    assert result["score"] == pytest.approx(0.6)
    assert result["window_count"] == 2
    assert len(result["score_trend"]) == 2
    assert result["key_frames"][0]["score"] == pytest.approx(0.8)
    assert all(call["frame_count"] == 16 for call in scorer.calls)
    assert [event.event_type for event in recorder.events] == [
        "creative_evaluation_started",
        "creative_frame_window_scored",
        "creative_frame_window_scored",
        "creative_evaluation_completed",
    ]


@pytest.mark.anyio
async def test_creative_evaluator_requires_calibrated_threshold(tmp_path: Path) -> None:
    """A valid MineCLIP score remains inconclusive until a reviewed threshold exists."""

    evaluator = CreativeTaskEvaluator(SequenceScorer([0.9]))  # type: ignore[arg-type]

    result = await evaluator.evaluate(_task_spec(threshold=None), _frames(tmp_path, 16), run_id="run-2")

    assert result["success"] is False
    assert result["inconclusive"] is True
    assert result["score"] == pytest.approx(0.9)
    assert result["score_threshold"] is None


@pytest.mark.anyio
async def test_programmatic_verifier_marks_creative_task_inconclusive() -> None:
    """Live training must not convert an unevaluated creative task into a normal failure."""

    result = await ProgrammaticVerifier().verify(_task_spec(threshold=None), {"steps": []})

    assert result["success"] is False
    assert result["inconclusive"] is True
    assert result["checks"][0]["external_evaluator"] == "mineclip"


@pytest.mark.anyio
async def test_mineclip_http_adapter_validates_and_parses_service_response() -> None:
    """The harness adapter sends exactly 16 encoded frames and validates score metadata."""

    async def handler(request: httpx.Request) -> httpx.Response:
        """Return one official-scorer-shaped response for the HTTP adapter."""

        payload = __import__("json").loads(request.content)
        assert len(payload["frames"]) == 16
        assert payload["prompt"] == "Build a stone tower"
        return httpx.Response(
            200,
            json={
                "target_probability": 0.75,
                "logits": [2.0, 1.0],
                "probabilities": [0.75, 0.25],
                "scorer": "mineclip_official",
                "variant": "attn",
                "checkpoint_checksum": "verified-checksum",
                "latency_ms": 12.5,
                "metadata": {"device": "cpu"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
    scorer = MineClipScorer("http://test", client=client)

    score = await scorer.score([b"frame"] * 16, "Build a stone tower", ["Create a garden"])
    await client.aclose()

    assert score.target_probability == pytest.approx(0.75)
    assert score.scorer == "mineclip_official"
    assert score.variant == "attn"
