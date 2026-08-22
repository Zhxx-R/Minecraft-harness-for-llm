from __future__ import annotations

import json
import subprocess
from pathlib import Path

import mc_agent_harness.evaluation.video as video_module
from mc_agent_harness.evaluation.video import validate_video_artifact


def test_video_validation_requires_decodable_dimensions(tmp_path: Path, monkeypatch) -> None:
    """A technically valid recording carries duration, dimensions, and codec evidence."""

    video = tmp_path / "agent.mp4"
    video.write_bytes(b"video-payload")
    monkeypatch.setattr(video_module.shutil, "which", lambda _name: "/usr/bin/ffprobe")
    monkeypatch.setattr(
        video_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {"codec_name": "h264", "width": 1280, "height": 720}
                    ],
                    "format": {"duration": "12.5"},
                }
            ),
            stderr="",
        ),
    )

    result = validate_video_artifact(video)

    assert result.valid is True
    assert result.duration_sec == 12.5
    assert result.width == 1280
    assert result.reasons == ()
