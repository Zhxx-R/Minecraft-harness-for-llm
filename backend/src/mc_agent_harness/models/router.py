from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from mc_agent_harness.core.config import settings
from mc_agent_harness.schemas.action import ActionDecision, HarnessAction, KnowledgeNeed


@dataclass(slots=True)
class ModelProfile:
    """Capability profile for one configured LLM provider/model."""

    id: str
    provider: str
    vision: bool = False
    tool_json: bool = True
    base_url: str | None = None
    api_key: str | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Token usage reported by the model provider."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    """Raw text completion and provider metadata before action validation."""

    content: str
    usage: ModelUsage = field(default_factory=ModelUsage)
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelActionResult:
    """Validated action produced by the model plus raw audit data."""

    action: HarnessAction
    raw_content: str
    usage: ModelUsage
    raw_response: dict[str, Any] = field(default_factory=dict)
    decision: ActionDecision | None = None


@dataclass(frozen=True, slots=True)
class ModelJSONResult:
    """Validated JSON object produced by the model plus raw audit data."""

    payload: dict[str, Any]
    raw_content: str
    usage: ModelUsage
    raw_response: dict[str, Any] = field(default_factory=dict)


class ModelRouterError(RuntimeError):
    """Raised when model routing, provider calls, or action parsing fails."""

    def __init__(
        self,
        message: str,
        raw_content: str | None = None,
        usage: ModelUsage | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_content = raw_content
        self.usage = usage or ModelUsage()
        self.raw_response = raw_response or {}


class ModelRouterTimeout(ModelRouterError):
    """Raised when a model provider times out before returning an action."""


class ModelProviderHTTPError(ModelRouterError):
    """Structured HTTP failure returned by a remote model provider."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after_sec: float | None,
        raw_response: dict[str, Any],
    ) -> None:
        super().__init__(message, raw_response=raw_response)
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec

    @property
    def transient(self) -> bool:
        """Return whether retrying this HTTP status is normally safe."""

        return self.status_code == 429 or 500 <= self.status_code < 600


class ModelProvider(Protocol):
    """Provider contract used by ModelRouter."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Return one raw model completion for prepared messages."""

        ...


class OpenAICompatibleProvider:
    """Provider adapter for OpenAI-compatible chat completion APIs."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Call a chat completions endpoint and return the first text message."""

        if not profile.base_url:
            raise ModelRouterError(f"Model profile {profile.id} is missing base_url.")
        if not profile.api_key:
            raise ModelRouterError(f"Model profile {profile.id} is missing api_key.")

        payload: dict[str, Any] = {
            "model": profile.id,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if response_schema is not None:
            payload["tools"] = [{"type": "function", "function": response_schema}]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{profile.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {profile.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                raw = response.json()
        except httpx.TimeoutException as exc:
            raise ModelRouterTimeout(
                f"Model provider timed out after {self.timeout:.3f}s.",
                raw_response={
                    "provider_error": type(exc).__name__,
                    "timeout_sec": self.timeout,
                },
            ) from exc
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            retry_after_sec = _retry_after_seconds(exc.response.headers.get("Retry-After"))
            raise ModelProviderHTTPError(
                f"Model provider returned HTTP {status_code}.",
                status_code=status_code,
                retry_after_sec=retry_after_sec,
                raw_response={
                    "provider_error": type(exc).__name__,
                    "status_code": status_code,
                    "retry_after_sec": retry_after_sec,
                    "response_excerpt": exc.response.text[:1000],
                },
            ) from exc
        except httpx.TransportError as exc:
            raise ModelRouterTimeout(
                f"Model provider transport failed: {exc}",
                raw_response={"provider_error": type(exc).__name__},
            ) from exc

        choice = raw["choices"][0]
        message = choice.get("message", {})
        content = message.get("content") or ""
        usage = raw.get("usage", {})
        return ModelCompletion(
            content=content,
            usage=ModelUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            ),
            raw_response=raw,
        )


class ResilientModelProvider:
    """Shared model provider with bounded concurrency and transient HTTP retries."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        *,
        max_concurrency: int = 2,
        transient_retries: int = 2,
        backoff_sec: tuple[float, ...] = (2.0, 5.0),
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive.")
        if transient_retries < 0:
            raise ValueError("transient_retries must be non-negative.")
        self.provider = provider or OpenAICompatibleProvider()
        self.max_concurrency = max_concurrency
        self.transient_retries = transient_retries
        self.backoff_sec = backoff_sec
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Call the provider with shared concurrency and retry transient HTTP failures."""

        failures: list[dict[str, Any]] = []
        for attempt_index in range(self.transient_retries + 1):
            try:
                async with self._semaphore:
                    completion = await self.provider.complete(
                        messages,
                        profile,
                        response_schema=response_schema,
                    )
            except ModelProviderHTTPError as exc:
                failures.append(
                    {
                        "attempt_index": attempt_index,
                        "status_code": exc.status_code,
                        "retry_after_sec": exc.retry_after_sec,
                    }
                )
                if not exc.transient or attempt_index >= self.transient_retries:
                    if not exc.transient:
                        raise
                    raise ModelRouterTimeout(
                        "Model provider transient HTTP retries were exhausted.",
                        raw_response={
                            **exc.raw_response,
                            "transient_http_retry_exhausted": True,
                            "provider_attempts": failures,
                            "max_concurrency": self.max_concurrency,
                        },
                    ) from exc
                delay_sec = (
                    exc.retry_after_sec
                    if exc.retry_after_sec is not None
                    else self._backoff(attempt_index)
                )
                if delay_sec > 0:
                    await asyncio.sleep(delay_sec)
                continue

            if failures:
                return replace(
                    completion,
                    raw_response={
                        **completion.raw_response,
                        "harness_provider_recovery": {
                            "provider_attempts": failures,
                            "recovered": True,
                            "max_concurrency": self.max_concurrency,
                        },
                    },
                )
            return completion
        raise AssertionError("unreachable provider retry state")

    def _backoff(self, attempt_index: int) -> float:
        """Return the configured transient-provider retry delay."""

        if not self.backoff_sec:
            return 0.0
        index = min(attempt_index, len(self.backoff_sec) - 1)
        return max(0.0, float(self.backoff_sec[index]))


class ModelRouter:
    """Routes model calls by capability profile and records usage metadata."""

    def __init__(
        self,
        default_model: str | None = None,
        provider: ModelProvider | None = None,
        profiles: dict[str, ModelProfile] | None = None,
    ) -> None:
        self.default_model = default_model or settings.model_default
        self.provider = provider or OpenAICompatibleProvider()
        self.profiles = profiles or {
            self.default_model: ModelProfile(
                id=self.default_model,
                provider="qwen",
                vision=True,
                tool_json=True,
                base_url=settings.qwen_base_url,
                api_key=settings.qwen_api_key,
            )
        }

    async def generate_action(
        self,
        messages: list[dict[str, Any]],
        model_id: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelActionResult:
        """Generate one structured action from prepared context messages."""

        profile = self.profiles.get(model_id or self.default_model)
        if profile is None:
            raise ModelRouterError(f"Unknown model profile: {model_id or self.default_model}")
        request_vision_input = _messages_have_image(messages)
        if request_vision_input and not profile.vision:
            raise ModelRouterError(f"Model profile {profile.id} does not allow image input.")

        try:
            completion = await self.provider.complete(
                messages,
                profile,
                response_schema=response_schema,
            )
        except ModelRouterTimeout as exc:
            raise ModelRouterTimeout(
                str(exc),
                raw_content=exc.raw_content,
                usage=exc.usage,
                raw_response={
                    **_model_audit_metadata(
                        profile,
                        exc.raw_response,
                        request_vision_input=request_vision_input,
                    ),
                    "timeout": True,
                },
            ) from exc
        except TimeoutError as exc:
            raise ModelRouterTimeout(
                f"Model provider timed out: {exc}",
                raw_response={
                    **_model_audit_metadata(
                        profile,
                        {},
                        request_vision_input=request_vision_input,
                    ),
                    "timeout": True,
                    "provider_error": type(exc).__name__,
                },
            ) from exc
        except ModelRouterError as exc:
            raise ModelRouterError(
                str(exc),
                raw_content=exc.raw_content,
                usage=exc.usage,
                raw_response=_model_audit_metadata(
                    profile,
                    exc.raw_response,
                    request_vision_input=request_vision_input,
                ),
            ) from exc
        audit_metadata = _model_audit_metadata(
            profile,
            completion.raw_response,
            request_vision_input=request_vision_input,
        )
        try:
            decision = parse_action_decision(completion.content)
        except ModelRouterError as exc:
            raise ModelRouterError(
                str(exc),
                raw_content=completion.content,
                usage=completion.usage,
                raw_response=audit_metadata,
            ) from exc
        return ModelActionResult(
            action=decision.action,
            raw_content=completion.content,
            usage=completion.usage,
            raw_response=audit_metadata,
            decision=decision,
        )

    async def generate_json(
        self,
        messages: list[dict[str, Any]],
        model_id: str | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelJSONResult:
        """Generate one structured JSON object for non-action model calls."""

        profile = self.profiles.get(model_id or self.default_model)
        if profile is None:
            raise ModelRouterError(f"Unknown model profile: {model_id or self.default_model}")
        request_vision_input = _messages_have_image(messages)
        if request_vision_input and not profile.vision:
            raise ModelRouterError(f"Model profile {profile.id} does not allow image input.")

        try:
            completion = await self.provider.complete(
                messages,
                profile,
                response_schema=response_schema,
            )
        except ModelRouterTimeout as exc:
            raise ModelRouterTimeout(
                str(exc),
                raw_content=exc.raw_content,
                usage=exc.usage,
                raw_response={
                    **_model_audit_metadata(
                        profile,
                        exc.raw_response,
                        request_vision_input=request_vision_input,
                    ),
                    "timeout": True,
                },
            ) from exc
        except TimeoutError as exc:
            raise ModelRouterTimeout(
                f"Model provider timed out: {exc}",
                raw_response={
                    **_model_audit_metadata(
                        profile,
                        {},
                        request_vision_input=request_vision_input,
                    ),
                    "timeout": True,
                    "provider_error": type(exc).__name__,
                },
            ) from exc
        except ModelRouterError as exc:
            raise ModelRouterError(
                str(exc),
                raw_content=exc.raw_content,
                usage=exc.usage,
                raw_response=_model_audit_metadata(
                    profile,
                    exc.raw_response,
                    request_vision_input=request_vision_input,
                ),
            ) from exc
        audit_metadata = _model_audit_metadata(
            profile,
            completion.raw_response,
            request_vision_input=request_vision_input,
        )
        try:
            payload = json.loads(_strip_markdown_fence(completion.content))
        except json.JSONDecodeError as exc:
            raise ModelRouterError(
                f"Model did not return valid JSON: {exc}",
                raw_content=completion.content,
                usage=completion.usage,
                raw_response=audit_metadata,
            ) from exc
        if not isinstance(payload, dict):
            raise ModelRouterError(
                "Model JSON must be an object.",
                raw_content=completion.content,
                usage=completion.usage,
                raw_response=audit_metadata,
            )
        return ModelJSONResult(
            payload=payload,
            raw_content=completion.content,
            usage=completion.usage,
            raw_response=audit_metadata,
        )


def parse_harness_action(content: str) -> HarnessAction:
    """Parse and validate a HarnessAction from model text."""

    return parse_action_decision(content).action


def parse_action_decision(content: str) -> ActionDecision:
    """Parse either the preferred decision envelope or a legacy action object."""

    try:
        payload = json.loads(_strip_markdown_fence(content))
    except json.JSONDecodeError as exc:
        raise ModelRouterError(f"Model did not return valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelRouterError("Model JSON must be an object.")

    if isinstance(payload.get("action"), dict):
        try:
            return ActionDecision.model_validate(payload)
        except ValidationError as exc:
            raise ModelRouterError(f"Model JSON is not a valid ActionDecision: {exc}") from exc

    try:
        action = HarnessAction.model_validate(payload)
    except ValidationError as exc:
        raise ModelRouterError(f"Model JSON is not a valid HarnessAction: {exc}") from exc
    return ActionDecision(
        reasoning_summary="",
        evidence=[],
        knowledge_need=KnowledgeNeed(needed=False),
        action=action,
    )


def _strip_markdown_fence(content: str) -> str:
    """Remove a simple Markdown JSON code fence if the model adds one."""

    stripped = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a numeric HTTP Retry-After header into non-negative seconds."""

    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _model_audit_metadata(
    profile: ModelProfile,
    raw_response: dict[str, Any],
    *,
    request_vision_input: bool,
) -> dict[str, Any]:
    """Extract only stable provider metadata needed to audit a model call."""

    metadata: dict[str, Any] = {
        "request_model": profile.id,
        "provider": profile.provider,
        "profile_vision": profile.vision,
        "request_vision_input": request_vision_input,
    }
    response_id = raw_response.get("id")
    response_model = raw_response.get("model")
    if response_id is not None:
        metadata["response_id"] = response_id
    if response_model is not None:
        metadata["response_model"] = response_model
    if raw_response.get("object") is not None:
        metadata["response_object"] = raw_response["object"]
    if raw_response.get("created") is not None:
        metadata["response_created"] = raw_response["created"]
    if raw_response.get("system_fingerprint") is not None:
        metadata["system_fingerprint"] = raw_response["system_fingerprint"]
    for key in (
        "provider_error",
        "status_code",
        "retry_after_sec",
        "timeout_sec",
        "harness_provider_recovery",
        "transient_http_retry_exhausted",
        "provider_attempts",
        "max_concurrency",
    ):
        if raw_response.get(key) is not None:
            metadata[key] = raw_response[key]
    return metadata


def _messages_have_image(messages: list[dict[str, Any]]) -> bool:
    """Return whether prepared model messages contain an OpenAI-compatible image part."""

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False
