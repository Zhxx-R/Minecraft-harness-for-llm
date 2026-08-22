from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
import sys
import threading
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field, field_validator


EXPECTED_CHECKSUMS = {
    "attn": "b5ece9198337cfd117a3bfbd921e56da",
    "avg": "d97a07f2830095a2016a8da22abcff52",
}
POOL_TYPES = {
    "attn": "attn.d2.nh8.glusw",
    "avg": "avg",
}
CLIP_LENGTH = 16
FRAME_HEIGHT = 160
FRAME_WIDTH = 256


class ScoreRequest(BaseModel):
    """Bounded JSON request for one MineCLIP video-text comparison."""

    frames: list[str] = Field(min_length=CLIP_LENGTH, max_length=CLIP_LENGTH)
    prompt: str = Field(min_length=1, max_length=2048)
    negative_prompts: list[str] = Field(min_length=1, max_length=31)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject whitespace-only target prompts."""

        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value.strip()

    @field_validator("negative_prompts")
    @classmethod
    def validate_negative_prompts(cls, values: list[str]) -> list[str]:
        """Reject blank contrast prompts and trim stable inputs."""

        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("negative_prompts must not contain blanks")
        return normalized


class ScoreResponse(BaseModel):
    """MineCLIP logits and normalized target probability for one clip."""

    target_probability: float
    logits: list[float]
    probabilities: list[float]
    scorer: str
    variant: str
    checkpoint_checksum: str
    latency_ms: float
    metadata: dict[str, Any]


class MineClipModelRuntime:
    """Lazy, process-local owner for the official MineCLIP model and checkpoint."""

    def __init__(self) -> None:
        """Read deployment settings without importing Torch during module discovery."""

        self.variant = os.getenv("MINECLIP_VARIANT", "attn").strip().lower()
        self.checkpoint_setting = os.getenv("MINECLIP_CHECKPOINT", "").strip()
        self.checkpoint_path = Path(self.checkpoint_setting) if self.checkpoint_setting else None
        self.repository_path = Path(os.getenv("MINECLIP_REPOSITORY", "vendor/MineCLIP"))
        self.device_name = os.getenv("MINECLIP_DEVICE", "auto").strip().lower()
        self.model: Any | None = None
        self.torch: Any | None = None
        self.device: Any | None = None
        self.checkpoint_checksum: str | None = None
        self.error: str | None = None
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def load(self) -> None:
        """Load and validate official model code and weights exactly once."""

        with self._load_lock:
            if self.model is not None:
                return
            try:
                self._load_model()
                self.error = None
            except Exception as exc:  # noqa: BLE001 - health endpoint must expose setup failures.
                self.error = f"{type(exc).__name__}: {exc}"
                raise

    def score(self, request: ScoreRequest) -> ScoreResponse:
        """Run synchronized inference for one fixed-length first-person clip."""

        self.load()
        assert self.model is not None
        assert self.torch is not None
        assert self.device is not None
        assert self.checkpoint_checksum is not None
        started = time.perf_counter()
        frames = np.stack([_decode_frame(frame) for frame in request.frames], axis=0)
        video = self.torch.from_numpy(frames).unsqueeze(0).to(self.device)
        prompts = [request.prompt, *request.negative_prompts]
        with self._inference_lock, self.torch.inference_mode():
            logits, _ = self.model(video, text_tokens=prompts, is_video_features=False)
            probabilities = self.torch.softmax(logits, dim=1)
        logits_values = logits[0].detach().float().cpu().tolist()
        probability_values = probabilities[0].detach().float().cpu().tolist()
        return ScoreResponse(
            target_probability=float(probability_values[0]),
            logits=[float(value) for value in logits_values],
            probabilities=[float(value) for value in probability_values],
            scorer="mineclip_official",
            variant=self.variant,
            checkpoint_checksum=self.checkpoint_checksum,
            latency_ms=(time.perf_counter() - started) * 1000,
            metadata={
                "device": str(self.device),
                "clip_length": CLIP_LENGTH,
                "resolution": [FRAME_HEIGHT, FRAME_WIDTH],
                "prompt_count": len(prompts),
            },
        )

    def health(self) -> dict[str, Any]:
        """Return readiness without leaking filesystem secrets beyond configured paths."""

        return {
            "status": "ready" if self.model is not None else "not_ready",
            "variant": self.variant,
            "device": str(self.device) if self.device is not None else self.device_name,
            "checkpoint_configured": self.checkpoint_path is not None,
            "checkpoint_checksum": self.checkpoint_checksum,
            "repository_configured": self.repository_path.is_dir(),
            "error": self.error,
        }

    def _load_model(self) -> None:
        """Build MineCLIP while bypassing unrelated MineDojo environment imports."""

        if self.variant not in POOL_TYPES:
            raise ValueError(f"Unsupported MineCLIP variant: {self.variant!r}.")
        if self.checkpoint_path is None:
            raise ValueError("MINECLIP_CHECKPOINT is not configured.")
        if not self.checkpoint_path.is_file():
            raise FileNotFoundError(f"MineCLIP checkpoint not found: {self.checkpoint_path}.")
        package_dir = self.repository_path / "mineclip"
        if not package_dir.is_dir():
            raise FileNotFoundError(f"Official MineCLIP repository not found: {self.repository_path}.")
        checksum = _checkpoint_md5(self.checkpoint_path)
        expected = EXPECTED_CHECKSUMS[self.variant]
        if checksum != expected:
            raise ValueError(
                f"MineCLIP checkpoint checksum mismatch for {self.variant}: {checksum} != {expected}."
            )

        package = types.ModuleType("mineclip")
        package.__path__ = [str(package_dir)]
        sys.modules["mineclip"] = package
        import torch
        from mineclip.mineclip import MineCLIP

        device = _select_device(torch, self.device_name)
        model = MineCLIP(
            arch="vit_base_p16_fz.v2.t2",
            hidden_dim=512,
            image_feature_dim=512,
            mlp_adapter_spec="v0-2.t0",
            pool_type=POOL_TYPES[self.variant],
            resolution=(FRAME_HEIGHT, FRAME_WIDTH),
        )
        try:
            checkpoint = torch.load(self.checkpoint_path, map_location=device, weights_only=False)
        except TypeError:
            checkpoint = torch.load(self.checkpoint_path, map_location=device)
        model.load_ckpt(checkpoint, strict=True)
        model.to(device).eval()
        self.torch = torch
        self.device = device
        self.model = model
        self.checkpoint_checksum = checksum


def _decode_frame(encoded: str) -> np.ndarray:
    """Decode, RGB-normalize, and resize one base64 JPEG/PNG into CHW uint8."""

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("frame is not valid base64") from exc
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError("frame must contain between 1 byte and 2 MiB")
    with Image.open(io.BytesIO(raw)) as image:
        rgb = image.convert("RGB").resize((FRAME_WIDTH, FRAME_HEIGHT), Image.Resampling.LANCZOS)
        array = np.asarray(rgb, dtype=np.uint8)
    return np.transpose(array, (2, 0, 1))


def _checkpoint_md5(path: Path) -> str:
    """Stream the upstream MD5 contract without duplicating a large checkpoint in memory."""

    digest = hashlib.md5()  # noqa: S324 - the official checkpoint identity uses MD5.
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _select_device(torch: Any, requested: str) -> Any:
    """Resolve CUDA, Apple MPS, or CPU while honoring explicit deployment settings."""

    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


runtime = MineClipModelRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Attempt eager model loading while preserving a diagnostic health endpoint."""

    try:
        runtime.load()
    except Exception:
        pass
    yield


app = FastAPI(title="MineCLIP Scorer", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    """Expose model readiness and verified checkpoint identity."""

    return runtime.health()


@app.post("/score", response_model=ScoreResponse)
def score(request: ScoreRequest) -> ScoreResponse:
    """Score one clip or return a setup-safe service error."""

    try:
        return runtime.score(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - service boundary converts setup failures.
        raise HTTPException(status_code=503, detail=f"MineCLIP unavailable: {exc}") from exc
