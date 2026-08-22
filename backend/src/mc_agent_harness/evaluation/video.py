from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mc_agent_harness.evaluation.creative import FrameArtifact


class FrameExtractionError(RuntimeError):
    """Raised when ffmpeg cannot decode an evaluation video into frames."""


@dataclass(frozen=True, slots=True)
class VideoArtifactValidation:
    """Technical validity evidence for one recorded creative-task video."""

    valid: bool
    path: str
    size_bytes: int
    duration_sec: float | None
    width: int | None
    height: int | None
    codec_name: str | None
    reasons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        """Convert validation evidence into a JSON-safe audit payload."""

        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        return payload


def validate_video_artifact(
    video_path: str | Path,
    *,
    minimum_duration_sec: float = 1.0,
    minimum_width: int = 320,
    minimum_height: int = 180,
) -> VideoArtifactValidation:
    """Probe a video and reject missing, truncated, or implausibly small captures."""

    video = Path(video_path).expanduser().resolve()
    reasons: list[str] = []
    if not video.is_file():
        return VideoArtifactValidation(
            valid=False,
            path=str(video),
            size_bytes=0,
            duration_sec=None,
            width=None,
            height=None,
            codec_name=None,
            reasons=("video_missing",),
        )
    size_bytes = video.stat().st_size
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return VideoArtifactValidation(
            valid=False,
            path=str(video),
            size_bytes=size_bytes,
            duration_sec=None,
            width=None,
            height=None,
            codec_name=None,
            reasons=("ffprobe_unavailable",),
        )
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height:format=duration",
        "-of",
        "json",
        str(video),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        reasons.append("video_probe_failed")
        return VideoArtifactValidation(
            valid=False,
            path=str(video),
            size_bytes=size_bytes,
            duration_sec=None,
            width=None,
            height=None,
            codec_name=None,
            reasons=tuple(reasons),
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        stream = streams[0] if isinstance(streams, list) and streams else {}
        format_payload = payload.get("format") if isinstance(payload, dict) else {}
        width = int(stream.get("width")) if stream.get("width") is not None else None
        height = int(stream.get("height")) if stream.get("height") is not None else None
        duration = (
            float(format_payload.get("duration"))
            if isinstance(format_payload, dict) and format_payload.get("duration") is not None
            else None
        )
        codec_name = str(stream.get("codec_name")) if stream.get("codec_name") else None
    except (TypeError, ValueError, json.JSONDecodeError, IndexError):
        width = None
        height = None
        duration = None
        codec_name = None
        reasons.append("video_probe_invalid_metadata")
    if size_bytes <= 0:
        reasons.append("video_empty")
    if duration is None or duration < minimum_duration_sec:
        reasons.append("video_too_short")
    if width is None or width < minimum_width or height is None or height < minimum_height:
        reasons.append("video_dimensions_invalid")
    if codec_name is None:
        reasons.append("video_codec_missing")
    return VideoArtifactValidation(
        valid=not reasons,
        path=str(video),
        size_bytes=size_bytes,
        duration_sec=duration,
        width=width,
        height=height,
        codec_name=codec_name,
        reasons=tuple(reasons),
    )


def extract_video_frames(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    sample_fps: float = 2.0,
    max_frames: int = 4096,
) -> list[FrameArtifact]:
    """Extract normalized 256x160 JPEG frames from a Minecraft recording with ffmpeg."""

    video = Path(video_path)
    output = Path(output_dir)
    if not video.is_file():
        raise FileNotFoundError(f"Evaluation video not found: {video}.")
    if sample_fps <= 0 or max_frames < 16:
        raise ValueError("sample_fps must be positive and max_frames must be at least 16.")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FrameExtractionError("ffmpeg was not found on PATH.")
    output.mkdir(parents=True, exist_ok=True)
    for existing in output.glob("frame_*.jpg"):
        existing.unlink()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps={sample_fps},scale=256:160:flags=lanczos",
        "-frames:v",
        str(max_frames),
        "-q:v",
        "2",
        str(output / "frame_%06d.jpg"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise FrameExtractionError(completed.stderr.strip() or "ffmpeg frame extraction failed.")
    return discover_frame_artifacts(output)


def discover_frame_artifacts(directory: str | Path) -> list[FrameArtifact]:
    """List image files in deterministic capture order for creative evaluation."""

    root = Path(directory)
    extensions = {".jpg", ".jpeg", ".png", ".webp"}
    paths = sorted(path for path in root.iterdir() if path.is_file() and path.suffix.lower() in extensions)
    return [FrameArtifact(path=path.resolve(), sequence=index) for index, path in enumerate(paths)]
