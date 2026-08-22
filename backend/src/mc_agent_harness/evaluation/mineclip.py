from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any

import httpx


MINECLIP_CLIP_LENGTH = 16
MINECLIP_MAX_FRAME_BYTES = 2 * 1024 * 1024
MINECLIP_MAX_REQUEST_BYTES = 32 * 1024 * 1024


class MineClipScorerError(RuntimeError):
    """Raised when the isolated MineCLIP service rejects or cannot score a clip."""


@dataclass(frozen=True, slots=True)
class MineClipScore:
    """Normalized score returned for one MineCLIP video window."""

    target_probability: float
    prompt: str
    negative_prompts: tuple[str, ...]
    logits: tuple[float, ...] = ()
    probabilities: tuple[float, ...] = ()
    scorer: str = "mineclip"
    variant: str | None = None
    checkpoint_checksum: str | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Convert one window score into a JSON-safe audit payload."""

        return asdict(self)


class MineClipScorer:
    """HTTP adapter for an isolated official MineCLIP inference service."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8091",
        *,
        timeout_sec: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure the scorer endpoint without loading Torch into the harness process."""

        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._client = client

    async def score(
        self,
        frames: list[bytes],
        prompt: str,
        negative_prompts: list[str] | tuple[str, ...],
    ) -> MineClipScore:
        """Score one 16-frame video window against a target and contrast prompts."""

        _validate_score_request(frames, prompt, negative_prompts)
        payload = {
            "frames": [base64.b64encode(frame).decode("ascii") for frame in frames],
            "prompt": prompt.strip(),
            "negative_prompts": [str(value).strip() for value in negative_prompts],
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_sec)
        try:
            response = await client.post(f"{self.base_url}/score", json=payload)
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MineClipScorerError(f"MineCLIP scorer request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        return _parse_score_response(result, prompt, tuple(negative_prompts))

    async def health(self) -> dict[str, Any]:
        """Return scorer readiness metadata for setup diagnostics."""

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=min(self.timeout_sec, 10.0))
        try:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MineClipScorerError(f"MineCLIP health request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        if not isinstance(payload, dict):
            raise MineClipScorerError("MineCLIP health endpoint returned a non-object response.")
        return payload


def _validate_score_request(
    frames: list[bytes],
    prompt: str,
    negative_prompts: list[str] | tuple[str, ...],
) -> None:
    """Enforce payload and contrast-set limits before crossing the process boundary."""

    if len(frames) != MINECLIP_CLIP_LENGTH:
        raise ValueError(f"MineCLIP requires exactly {MINECLIP_CLIP_LENGTH} frames per window.")
    if not prompt.strip():
        raise ValueError("MineCLIP prompt must not be empty.")
    if not negative_prompts or any(not str(value).strip() for value in negative_prompts):
        raise ValueError("MineCLIP requires at least one non-empty negative prompt.")
    total_bytes = 0
    for frame in frames:
        if not frame:
            raise ValueError("MineCLIP frames must not be empty.")
        if len(frame) > MINECLIP_MAX_FRAME_BYTES:
            raise ValueError("One MineCLIP frame exceeds the 2 MiB safety limit.")
        total_bytes += len(frame)
    if total_bytes > MINECLIP_MAX_REQUEST_BYTES:
        raise ValueError("MineCLIP frame payload exceeds the 32 MiB safety limit.")


def _parse_score_response(
    payload: Any,
    prompt: str,
    negative_prompts: tuple[str, ...],
) -> MineClipScore:
    """Validate and normalize one scorer-service JSON response."""

    if not isinstance(payload, dict):
        raise MineClipScorerError("MineCLIP scorer returned a non-object response.")
    target_probability = payload.get("target_probability")
    if not isinstance(target_probability, (int, float)) or not 0 <= float(target_probability) <= 1:
        raise MineClipScorerError("MineCLIP scorer returned an invalid target_probability.")
    logits = _numeric_tuple(payload.get("logits"))
    probabilities = _numeric_tuple(payload.get("probabilities"))
    expected_prompt_count = 1 + len(negative_prompts)
    if len(logits) != expected_prompt_count or len(probabilities) != expected_prompt_count:
        raise MineClipScorerError(
            "MineCLIP scorer returned logits/probabilities with the wrong prompt count."
        )
    if any(not isfinite(logit) for logit in logits):
        raise MineClipScorerError("MineCLIP scorer returned a non-finite logit.")
    if any(
        not isfinite(probability) or probability < 0 or probability > 1
        for probability in probabilities
    ):
        raise MineClipScorerError("MineCLIP scorer returned an invalid probability vector.")
    if abs(sum(probabilities) - 1.0) > 1e-4:
        raise MineClipScorerError("MineCLIP scorer probabilities do not sum to one.")
    if abs(probabilities[0] - float(target_probability)) > 1e-5:
        raise MineClipScorerError("MineCLIP target_probability does not match probabilities[0].")
    return MineClipScore(
        target_probability=float(target_probability),
        prompt=prompt,
        negative_prompts=negative_prompts,
        logits=logits,
        probabilities=probabilities,
        scorer=str(payload.get("scorer") or "mineclip"),
        variant=_optional_string(payload.get("variant")),
        checkpoint_checksum=_optional_string(payload.get("checkpoint_checksum")),
        latency_ms=_optional_float(payload.get("latency_ms")),
        metadata=dict(payload.get("metadata")) if isinstance(payload.get("metadata"), dict) else {},
    )


def _numeric_tuple(value: Any) -> tuple[float, ...]:
    """Convert an optional JSON numeric list into an immutable tuple."""

    if not isinstance(value, list):
        return ()
    return tuple(float(item) for item in value if isinstance(item, (int, float)))


def _optional_string(value: Any) -> str | None:
    """Return a non-empty string or None."""

    return str(value) if isinstance(value, str) and value else None


def _optional_float(value: Any) -> float | None:
    """Return a finite-looking numeric value or None."""

    return float(value) if isinstance(value, (int, float)) else None
