from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from mc_agent_harness.evaluation.mineclip import MineClipScore
from mc_agent_harness.evaluation.progress import (
    CreativeProgressFeedbackRuntime,
    CreativeProgressMonitor,
    CreativeProgressPolicy,
)
from mc_agent_harness.schemas.action import HarnessAction


@pytest.fixture
def anyio_backend() -> str:
    """Force progress monitor tests to use asyncio."""

    return "asyncio"


class FakeFrameProvider:
    """Fast trusted-frame provider that writes one small artifact per capture."""

    def __init__(self, root: Path) -> None:
        """Store generated frame artifacts under the test directory."""

        self.root = root
        self.sequence = 0

    async def capture(self) -> dict[str, Any]:
        """Create one deterministic fake JPEG payload for the ring-buffer sampler."""

        self.sequence += 1
        path = self.root / f"progress_{self.sequence:04d}.jpg"
        path.write_bytes(f"frame-{self.sequence}".encode("ascii"))
        return {"artifact_path": str(path), "image": str(path)}


class FakeScorer:
    """Sequence scorer that makes baseline and improvement results deterministic."""

    def __init__(self) -> None:
        """Initialize an empty list of scored frame windows."""

        self.calls: list[list[bytes]] = []

    async def score(
        self,
        frames: list[bytes],
        prompt: str,
        negative_prompts: list[str],
    ) -> MineClipScore:
        """Return a higher score after the baseline request."""

        self.calls.append(frames)
        score = 0.4 if len(self.calls) == 1 else 0.6
        return MineClipScore(
            target_probability=score,
            prompt=prompt,
            negative_prompts=tuple(negative_prompts),
            logits=(score, 1 - score),
            probabilities=(score, 1 - score),
            scorer="fake-mineclip",
            latency_ms=1.0,
        )


class FakeRuntime:
    """Minimal game runtime used to verify decorator timing and observation injection."""

    async def reset(self, task_spec: dict[str, Any]) -> dict[str, Any]:
        """Return reset metadata without external resources."""

        return {"task_id": task_spec.get("task_id")}

    async def observe(self) -> dict[str, Any]:
        """Return one stable structured observation."""

        return {"position": {"x": 0, "y": 64, "z": 0}, "inventory": []}

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Complete every fake action immediately."""

        return {"ok": True, "action_type": action.type}

    async def snapshot(self) -> dict[str, Any]:
        """Return a compact fake runtime snapshot."""

        return {"ok": True}

    async def close(self) -> None:
        """Close without external resources."""


def _creative_task() -> dict[str, Any]:
    """Build a creative task with the minimum MineCLIP verifier contract."""

    return {
        "task_id": "creative:test",
        "category": "creative",
        "verifier": {
            "type": "creative_mineclip",
            "prompt": "Build a small stone shelter",
            "negative_prompts": ["An empty field"],
        },
    }


@pytest.mark.anyio
async def test_important_action_returns_immediately_and_feedback_arrives_later(
    tmp_path: Path,
) -> None:
    """Important actions queue scoring without waiting for capture or MineCLIP inference."""

    scorer = FakeScorer()
    policy = CreativeProgressPolicy(
        sample_fps=500.0,
        post_action_frames=0,
        min_checkpoint_interval_sec=0.0,
        window_wait_timeout_sec=2.0,
    )
    monitor = CreativeProgressMonitor(  # type: ignore[arg-type]
        FakeFrameProvider(tmp_path),
        scorer,  # type: ignore[arg-type]
        policy=policy,
    )
    runtime = CreativeProgressFeedbackRuntime(FakeRuntime(), monitor)  # type: ignore[arg-type]
    await runtime.reset(_creative_task())

    await _wait_until(lambda: len(scorer.calls) >= 1)
    started = time.perf_counter()
    result = await runtime.act(HarnessAction(type="place_block", args={}))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert result["creative_progress_job"]["status"] == "queued"
    assert result["creative_progress_job"]["blocking"] is False

    await _wait_until(lambda: len(scorer.calls) >= 2)
    observation = await runtime.observe()
    latest = observation["creative_progress"]["latest"]
    assert latest["action_type"] == "place_block"
    assert latest["score"] == pytest.approx(0.6)
    assert latest["score_delta"] == pytest.approx(0.2)
    assert latest["trend"] == "improving"
    assert latest["advisory_only"] is True
    assert latest["success_authority"] == "human_review"
    assert all(len(frames) == 16 for frames in scorer.calls)
    await runtime.close()


@pytest.mark.anyio
async def test_noncreative_task_does_not_start_progress_feedback(tmp_path: Path) -> None:
    """Programmatic tasks keep their existing observation and action result contract."""

    monitor = CreativeProgressMonitor(  # type: ignore[arg-type]
        FakeFrameProvider(tmp_path),
        FakeScorer(),  # type: ignore[arg-type]
        policy=CreativeProgressPolicy(sample_fps=100.0),
    )
    runtime = CreativeProgressFeedbackRuntime(FakeRuntime(), monitor)  # type: ignore[arg-type]
    await runtime.reset({"task_id": "harvest:test", "category": "harvest"})

    result = await runtime.act(HarnessAction(type="place_block", args={}))
    observation = await runtime.observe()

    assert "creative_progress_job" not in result
    assert "creative_progress" not in observation
    await runtime.close()


async def _wait_until(predicate: Any, *, timeout_sec: float = 2.0) -> None:
    """Poll a synchronous predicate while background monitor tasks make progress."""

    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise TimeoutError("Timed out waiting for creative progress feedback.")
